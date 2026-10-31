"""``AccessContext``: what one user may reach, resolved lazily (plan §3.5, §3.6).

A context is built per request (``request.access``, by ``AccessContextMiddleware``) for
reads and the coarse view check, and per user inside ``authorize_in_txn`` for writes.
Nothing is read until a scope is asked for; the user's memberships, branch scopes and
assignments are then each read once per context. Validity (S8) and organization state
(S9) are evaluated at the database clock of those queries; inside a write transaction that
is the transaction's own clock.

Resolution per valid membership (``scope_for``):

- S8: the user is active and not offboarded; the membership is not revoked and
  ``valid_from ≤ now < valid_until``.
- S9: a branch role on an inactive branch, or a branch of an inactive company, grants
  nothing; a partner membership grants nothing while its company is inactive (F50).
- S2: ``GROUP_ADMIN`` reaches everything, inactive organizations included.
- S1: a branch-scoped role reaches its branch and the active branches it manages.
- S4: a site manager or partner reaches the projects assigned to them by valid
  assignments, while the project's own branch is active in effect.
- S6: one ``Grant`` per membership; ``Scope`` never merges them.
"""

from collections.abc import Callable
from dataclasses import dataclass
from functools import cached_property
from typing import Any, cast
from uuid import UUID

from django.db.models import Model

from apps.access.policy import is_platform_operator, mfa_satisfied, valid_now
from apps.access.principals import SessionPrincipal
from apps.access.roles import (
    ACTIVE_PROJECTS_ONLY,
    BRANCH_ROLES,
    COMPANY_ROLES,
    INSTALLATION_CAPABILITIES,
    ROLE_ASSIGNMENT_KIND,
    ROLE_CAPABILITIES,
    ROLE_SCOPE_BASIS,
    Capability,
    Reach,
    Role,
    ScopeBasis,
)
from apps.access.scopes import Grant, Scope, object_in_scope
from apps.accounts.models import User
from apps.organizations.models import Branch, Membership
from apps.projects.models import ProjectAssignment


@dataclass(frozen=True)
class _Effective:
    """A valid membership that S9 lets through: its role and, for branch roles, its branch."""

    role: str
    branch_id: UUID | None


class AccessContext:
    """The access of one user: ``scope_for``, ``has`` and ``can``.

    ``session`` builds the session principal of a request context (epoch, MFA time); a
    context built for a user alone has none.
    """

    def __init__(
        self, user: Any = None, *, session: Callable[[], SessionPrincipal] | None = None
    ) -> None:
        authenticated = user is not None and getattr(user, "is_authenticated", False)
        self.user: User | None = user if authenticated else None
        self._session = session if authenticated else None
        self._scopes: dict[str, Scope] = {}

    @classmethod
    def anonymous(cls) -> AccessContext:
        return cls()

    @classmethod
    def for_user(cls, user: User) -> AccessContext:
        return cls(user)

    @property
    def user_id(self) -> UUID | None:
        return self.user.pk if self.user is not None else None

    @property
    def is_authenticated(self) -> bool:
        return self.user is not None

    @cached_property
    def principal(self) -> SessionPrincipal:
        """The session principal for the services this request calls."""
        if self._session is None:
            raise ValueError("This access context has no session")
        return self._session()

    @cached_property
    def mfa_passed(self) -> bool:
        """This session completed MFA after the user's last privilege elevation."""
        if self._session is None or self.user is None:
            return False
        return mfa_satisfied(self.principal.mfa_at, self.user.privilege_elevated_at)

    @cached_property
    def is_platform_operator(self) -> bool:
        return is_platform_operator(self.user)

    def scope_for(self, capability: str) -> Scope:
        """What ``capability`` reaches, one grant per membership that grants it (S6)."""
        scope = self._scopes.get(capability)
        if scope is None:
            scope = Scope(capability, self._grants(capability), self.user_id)
            self._scopes[capability] = scope
        return scope

    def has(self, capability: str) -> bool:
        """Granted somewhere: the coarse view check."""
        return bool(self.scope_for(capability))

    def can(self, capability: str, obj: Model) -> bool:
        """``obj`` (as stored) is within ``capability``'s scope."""
        return object_in_scope(obj, self.scope_for(capability))

    # --- resolution ---------------------------------------------------------------------

    @cached_property
    def _memberships(self) -> tuple[_Effective, ...]:
        user = self.user
        if user is None or not user.is_active or user.offboarded_at is not None:
            return ()
        rows = Membership.objects.filter(valid_now(), user_id=user.pk).values_list(
            "role",
            "branch_id",
            "branch__is_active",
            "branch__company__is_active",
            "company__is_active",
        )
        effective = set()
        for role, branch_id, branch_active, branch_company_active, company_active in rows:
            if role in BRANCH_ROLES and not (branch_active and branch_company_active):
                continue
            if role in COMPANY_ROLES and not company_active:
                continue
            effective.add(_Effective(role, branch_id))
        return tuple(sorted(effective, key=lambda m: (m.role, str(m.branch_id))))

    @cached_property
    def _branch_scopes(self) -> dict[UUID, frozenset[UUID]]:
        """S1 per anchor branch: itself and the active branches it manages."""
        anchors = {
            cast(UUID, m.branch_id)
            for m in self._memberships
            if ROLE_SCOPE_BASIS[Role(m.role)] is ScopeBasis.BRANCH
        }
        scopes: dict[UUID, set[UUID]] = {anchor: {anchor} for anchor in anchors}
        managed = Branch.objects.filter(
            managed_by_id__in=anchors, is_active=True, company__is_active=True
        ).values_list("pk", "managed_by_id")
        for branch_id, manager_id in managed:
            scopes[manager_id].add(branch_id)
        return {anchor: frozenset(ids) for anchor, ids in scopes.items()}

    @cached_property
    def _assigned(self) -> dict[str, frozenset[UUID]]:
        """S4 per assignment kind: projects of valid assignments, own branch active.

        Only read for an assignment-scoped membership, so the user is known.
        """
        kinds = {
            ROLE_ASSIGNMENT_KIND[Role(m.role)]
            for m in self._memberships
            if m.role in ROLE_ASSIGNMENT_KIND
        }
        assigned: dict[str, set[UUID]] = {kind: set() for kind in kinds}
        rows = ProjectAssignment.objects.filter(
            valid_now(),
            user_id=cast(UUID, self.user_id),
            kind__in=kinds,
            project__branch__is_active=True,
            project__branch__company__is_active=True,
        ).values_list("kind", "project_id")
        for kind, project_id in rows:
            assigned[kind].add(project_id)
        return {kind: frozenset(ids) for kind, ids in assigned.items()}

    def _grants(self, capability: str) -> tuple[Grant, ...]:
        try:
            wanted = Capability(capability)
        except ValueError:
            return ()  # unknown capabilities are granted to nobody (fail closed)
        grants: dict[Grant, None] = {}
        for membership in self._memberships:
            role = Role(membership.role)
            reach = ROLE_CAPABILITIES[role].get(wanted)
            if reach is not None:
                grant = self._grant(role, membership.branch_id, wanted, reach)
                if not grant.is_empty:
                    grants[grant] = None
        return tuple(grants)

    def _grant(
        self, role: Role, branch_id: UUID | None, capability: Capability, reach: Reach
    ) -> Grant:
        basis = ROLE_SCOPE_BASIS[role]
        everything = basis is ScopeBasis.INSTALLATION
        installation = capability in INSTALLATION_CAPABILITIES
        if everything or installation:
            return Grant(role, reach, everything=everything, installation=installation)
        active_only = role in ACTIVE_PROJECTS_ONLY
        if basis is ScopeBasis.BRANCH:
            branches = self._branch_scopes[cast(UUID, branch_id)]
            return Grant(role, reach, branch_ids=branches, active_projects_only=active_only)
        projects = self._assigned[ROLE_ASSIGNMENT_KIND[role]]
        return Grant(role, reach, project_ids=projects, active_projects_only=active_only)

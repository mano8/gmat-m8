"""What a capability reaches: grants, scopes and scoped querysets (plan §3.5, §3.6).

A ``Scope`` is the answer to ``ctx.scope_for(capability)``: one ``Grant`` per membership
that grants the capability. Grants are never merged (S6): a capability granted by one
membership applies only inside that membership's scope, so each grant carries its own
branches, projects and reach. ``Scope`` turns them into ``Q`` objects:

- ``branch_q``: rows that are, or belong to, a branch of a branch-scoped grant (S1, S2);
- ``project_q``: projects of the grants' branches (S3) or assigned to the user (S4);
- ``document_q``: documents by their branch (S5) or project, own documents only for a
  ``Reach.OWN`` grant. Document models (M4 onwards) declare their fields through it.
  A ``Reach.SITE`` grant reaches a document only through its project, by the grant's
  project scope (S3, S4); for a ``SITE_CAPABILITIES`` capability every other grant reaches
  those documents too, on top of its own (Q44, ADR-0028: project receiving).

This module imports no model, so model modules can build their querysets on it.
"""

from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Protocol, Self
from uuid import UUID

from django.db.models import Model, Q, QuerySet

from apps.access.roles import (
    ACTIVE_PROJECT_STATUS,
    PRIVILEGED_ROLES,
    SITE_CAPABILITIES,
    Reach,
    Role,
)

ALWAYS = Q(pk__isnull=False)
NEVER = Q(pk__in=[])


def any_of(conditions: Iterable[Q]) -> Q:
    """OR the conditions; no condition matches nothing.

    ``Q()`` is not used for "everything": combined with ``|`` it is dropped, not matched.
    """
    result: Q | None = None
    for condition in conditions:
        result = condition if result is None else result | condition
    return NEVER if result is None else result


@dataclass(frozen=True)
class Grant:
    """What one membership grants for one capability.

    ``everything``: S2 (group administrators). ``installation``: master data shared by the
    whole installation (catalog, suppliers). ``branch_ids``: S1 after S9.
    ``project_ids``: the valid assignments of an assignment-scoped role (S4).
    """

    role: Role
    reach: Reach
    everything: bool = False
    installation: bool = False
    branch_ids: frozenset[UUID] = field(default_factory=frozenset)
    project_ids: frozenset[UUID] = field(default_factory=frozenset)
    active_projects_only: bool = False

    @property
    def privileged(self) -> bool:
        return self.role in PRIVILEGED_ROLES

    @property
    def is_empty(self) -> bool:
        return not (self.everything or self.installation or self.branch_ids or self.project_ids)

    def project_q(self, prefix: str) -> Q:
        if self.everything:
            return ALWAYS
        parts: list[Q] = []
        if self.branch_ids:
            ids = sorted(self.branch_ids)
            parts.append(
                Q(**{f"{prefix}branch__in": ids}) | Q(**{f"{prefix}procurement_branch__in": ids})
            )
        if self.project_ids:
            parts.append(Q(**{f"{prefix}pk__in": sorted(self.project_ids)}))
        condition = any_of(parts)
        if self.active_projects_only:
            condition &= Q(**{f"{prefix}status": ACTIVE_PROJECT_STATUS})
        return condition

    def site_q(self, project: str | None) -> Q:
        """Documents whose project (through ``project``) is in this grant's project scope:
        its branches' projects (S3) or its assigned ones (S4), active only where the role
        sees only active projects. A document without a project path is never one."""
        return NEVER if project is None else self.project_q(f"{project}__")

    def _document_parts(
        self, branches: tuple[str, ...], project: str | None, *, site: bool
    ) -> list[Q]:
        """By the document's branch (S5), its assigned project (S4) and, with ``site``, the
        projects of the grant's scope."""
        parts: list[Q] = []
        if self.branch_ids:
            ids = sorted(self.branch_ids)
            parts.extend(Q(**{f"{name}__in": ids}) for name in branches)
        if self.project_ids and project is not None:
            parts.append(Q(**{f"{project}__in": sorted(self.project_ids)}))
        if site:
            parts.append(self.site_q(project))
        return parts

    def document_q(
        self,
        branches: tuple[str, ...],
        project: str | None,
        owner: str | None,
        user_id: UUID | None,
        *,
        site: bool = False,
    ) -> Q:
        """``site``: the capability also reaches the documents of the grant's projects
        (``SITE_CAPABILITIES``); a ``Reach.SITE`` grant reaches only those."""
        if self.everything:
            condition = ALWAYS
        elif self.reach is Reach.SITE:
            condition = self.site_q(project)
        else:
            condition = any_of(self._document_parts(branches, project, site=site))
        if self.reach is Reach.OWN:
            if owner is None or user_id is None:
                return NEVER  # nothing is "own" without an owner
            condition &= Q(**{owner: user_id})
        return condition


@dataclass(frozen=True)
class Scope:
    """``ctx.scope_for(capability)``: the grants of every membership that grants it."""

    capability: str
    grants: tuple[Grant, ...] = ()
    user_id: UUID | None = None

    def __bool__(self) -> bool:
        return bool(self.grants)

    @property
    def everything(self) -> bool:
        return any(grant.everything for grant in self.grants)

    @property
    def installation(self) -> bool:
        """The capability reaches the installation-wide master data (catalog, suppliers)."""
        return any(grant.installation or grant.everything for grant in self.grants)

    def only(self, *, privileged: bool) -> Scope:
        """The grants of privileged roles, or of the others (plan §3.6, step 5)."""
        grants = tuple(grant for grant in self.grants if grant.privileged is privileged)
        return Scope(self.capability, grants, self.user_id)

    def of_roles(self, roles: Iterable[Role]) -> Scope:
        """The grants of the memberships of ``roles`` only (S6: never a role held elsewhere)."""
        wanted = frozenset(roles)
        grants = tuple(grant for grant in self.grants if grant.role in wanted)
        return Scope(self.capability, grants, self.user_id)

    def branch_q(self, field_name: str = "pk") -> Q:
        """Branch rows in scope (S1, S2), through ``field_name`` (``pk`` for ``Branch``).

        A ``Reach.SITE`` grant reaches no branch row: only its projects' worksites."""
        if self.everything:
            return ALWAYS
        return any_of(
            Q(**{f"{field_name}__in": sorted(grant.branch_ids)})
            for grant in self.grants
            if grant.branch_ids and grant.reach is not Reach.SITE
        )

    def project_q(self, prefix: str = "") -> Q:
        """Project rows in scope (S3, S4), through ``prefix`` (``""`` for ``Project``)."""
        return any_of(grant.project_q(prefix) for grant in self.grants)

    def document_q(
        self,
        *,
        branch: str | tuple[str, ...],
        project: str | None = None,
        owner: str | None = None,
    ) -> Q:
        """Documents in scope (S5): by branch, or by project for assigned projects.

        ``Reach.OWN`` grants add ``owner = user``; a model without ``owner`` gets none of
        their rows. Installation-wide grants reach no document. For a ``SITE_CAPABILITIES``
        capability, every grant also reaches the documents of its projects through
        ``project`` (Q44); a ``Reach.SITE`` grant only those.
        """
        branches = (branch,) if isinstance(branch, str) else branch
        site = self.capability in SITE_CAPABILITIES
        return any_of(
            grant.document_q(branches, project, owner, self.user_id, site=site)
            for grant in self.grants
            if grant.everything or not grant.installation
        )


class SupportsScope(Protocol):
    def scope_for(self, capability: str) -> Scope: ...


class ScopedQuerySet[M: Model](QuerySet[M]):
    """A model's rows filtered by scope; subclasses implement ``scope_q``.

    ``visible_to(ctx, capability)`` is the only way views, forms and selectors read a
    scoped model (CLAUDE.md, "Access"); ``authorize_in_txn`` uses the same filter for an
    object of a write, so reads and writes cannot disagree.
    """

    def scope_q(self, scope: Scope) -> Q:
        raise NotImplementedError

    def in_scope(self, scope: Scope) -> Self:
        if not scope:
            return self.none()
        return self.filter(self.scope_q(scope))

    def visible_to(self, ctx: SupportsScope, capability: str) -> Self:
        return self.in_scope(ctx.scope_for(capability))


class InstallationQuerySet[M: Model](ScopedQuerySet[M]):
    """Master data shared by the whole installation (catalog, suppliers; plan §4.2).

    A grant reaches all of it or none: no branch, project or ownership narrows it
    (``INSTALLATION_CAPABILITIES``). What a role may see of a row is decided by the
    selector's projection (suppliers: ``supplier.view``), never by the scope.
    """

    def scope_q(self, scope: Scope) -> Q:
        return ALWAYS if scope.installation else NEVER


def object_in_scope(obj: Model, scope: Scope) -> bool:
    """Whether ``obj`` (as stored) is in ``scope``; its model needs a ``ScopedQuerySet``."""
    queryset = type(obj)._default_manager.all()
    if not isinstance(queryset, ScopedQuerySet):
        raise TypeError(f"{type(obj).__name__} has no scoped queryset (visible_to)")
    return bool(scope) and queryset.in_scope(scope).filter(pk=obj.pk).exists()

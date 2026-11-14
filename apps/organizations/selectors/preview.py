"""What a scope-defining change does to access, read before it is made (M10.2, ADR-0030).

Deactivating or reactivating a company or a branch, and changing a branch's managing branch,
change who reaches what. Their dialog says it first: the branches that leave or join a
managing branch's scope, the valid memberships and project assignments whose reach changes,
the people who no longer reach any branch or project (or reach one again), and the open
documents that make the service refuse a deactivation. Reach here is branches and projects:
the installation-wide catalog, which any effective role reads, is left out, so a site manager
whose last project drops out "reaches nothing" though the catalog stays open to them.

The figures follow the resolution of ``apps.access.context`` on the organization as it is and
as it would be after the change: S8 (valid grants of active accounts), S9 (a branch grants
through its own memberships only while it and its company are active; a partner's membership
only while its company is), S1 (a branch-scoped role reaches its branch and the active
branches it manages) and S4 (assignments, while the project's branch is active; a partner's
active projects only). A reading, not a promise: the service checks its own preconditions
again under the ``AccessState`` lock, and ``generation`` lets it refuse a confirmation given
on an organization that changed since (``seen_generation``).
"""

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field, replace
from types import MappingProxyType
from uuid import UUID

from django.db.models import Q

from apps.access.context import AccessContext
from apps.access.exceptions import NotInScope
from apps.access.generations import org_generation
from apps.access.policy import valid_now
from apps.access.roles import (
    ACTIVE_PROJECT_STATUS,
    ACTIVE_PROJECTS_ONLY,
    BRANCH_ROLES,
    COMPANY_ROLES,
    ROLE_ASSIGNMENT_KIND,
    ROLE_SCOPE_BASIS,
    Capability,
    Role,
    ScopeBasis,
)
from apps.accounts.models import User
from apps.accounts.people import name_of
from apps.organizations.deactivation import OpenDocument, open_documents
from apps.organizations.models import Branch, Company, Membership
from apps.projects.models import ProjectAssignment

NAMES_SHOWN = 10
DOCUMENTS_SHOWN = 10

_BY_BRANCH = frozenset(
    role for role, basis in ROLE_SCOPE_BASIS.items() if basis is ScopeBasis.BRANCH
)
_BY_ASSIGNMENT = frozenset(ROLE_ASSIGNMENT_KIND)
_EVERYWHERE = "*"


@dataclass(frozen=True)
class BranchMove:
    """A branch entering or leaving the scope of the branch that manages it (S1)."""

    branch: str
    manager: str


@dataclass(frozen=True)
class ScopePreview:
    """What a change does, as the dialog shows it (labels and counts only)."""

    generation: int
    leaving: tuple[BranchMove, ...] = ()
    joining: tuple[BranchMove, ...] = ()
    stopping: tuple[str, ...] = ()  # branches whose own memberships stop granting
    starting: tuple[str, ...] = ()  # branches whose own memberships grant again
    memberships: int = 0
    assignments: int = 0
    # (id, name) of the first NAMES_SHOWN who reach no branch or project any more (losing),
    # or reach one again (regaining).
    losing: tuple[tuple[UUID, str], ...] = ()
    losing_total: int = 0
    regaining: tuple[tuple[UUID, str], ...] = ()
    regaining_total: int = 0
    blocking: tuple[OpenDocument, ...] = ()  # the first DOCUMENTS_SHOWN
    blocking_total: int = 0

    @property
    def changes_access(self) -> bool:
        return bool(
            self.leaving
            or self.joining
            or self.stopping
            or self.starting
            or self.memberships
            or self.assignments
        )

    @property
    def losing_more(self) -> int:
        return self.losing_total - len(self.losing)

    @property
    def regaining_more(self) -> int:
        return self.regaining_total - len(self.regaining)

    @property
    def blocking_more(self) -> int:
        return self.blocking_total - len(self.blocking)


# --- the organization as data --------------------------------------------------------------


@dataclass(frozen=True)
class _Branch:
    company_id: UUID
    active: bool
    managed_by: UUID | None
    label: str


@dataclass(frozen=True)
class _Org:
    """The scope-defining attributes of every branch and company."""

    branches: Mapping[UUID, _Branch]
    companies: Mapping[UUID, bool]

    def live(self, branch_id: UUID | None) -> bool:
        """S9: the branch and its company are active."""
        found = self.branches.get(branch_id) if branch_id is not None else None
        return found is not None and found.active and self.companies.get(found.company_id, False)

    def scopes(self) -> dict[UUID, frozenset[UUID]]:
        """S1 for every branch as an anchor: itself and the live branches it manages."""
        scopes: dict[UUID, set[UUID]] = {
            pk: ({pk} if self.live(pk) else set()) for pk in self.branches
        }
        for pk, found in self.branches.items():
            if found.managed_by is not None and self.live(found.managed_by) and self.live(pk):
                scopes[found.managed_by].add(pk)
        return {pk: frozenset(ids) for pk, ids in scopes.items()}

    def changed(
        self,
        *,
        companies: Mapping[UUID, bool] = MappingProxyType({}),
        active: Mapping[UUID, bool] = MappingProxyType({}),
        managers: Mapping[UUID, UUID | None] = MappingProxyType({}),
    ) -> _Org:
        branches = dict(self.branches)
        for pk, value in active.items():
            branches[pk] = replace(branches[pk], active=value)
        for pk, manager in managers.items():
            branches[pk] = replace(branches[pk], managed_by=manager)
        return _Org(branches, {**self.companies, **companies})


def _read_org() -> _Org:
    rows = Branch.objects.values_list(
        "pk", "company_id", "is_active", "managed_by_id", "company__code", "code", "name"
    )
    branches = {
        pk: _Branch(company, active, manager, f"{company_code}-{code} · {name}")
        for pk, company, active, manager, company_code, code, name in rows
    }
    return _Org(branches, dict(Company.objects.values_list("pk", "is_active")))


# --- one person's grants ----------------------------------------------------------------------


@dataclass(frozen=True)
class _Grant:
    pk: UUID
    role: Role
    branch_id: UUID | None
    company_id: UUID | None


@dataclass(frozen=True)
class _Assigned:
    pk: UUID
    kind: str
    project_id: UUID
    branch_id: UUID
    active_project: bool


@dataclass
class _Person:
    name: str
    grants: list[_Grant]
    assigned: list[_Assigned]


class _Reach:
    """What each grant and assignment of a person reaches in one state of the organization."""

    def __init__(self, org: _Org, scopes: _Scopes) -> None:
        self.org, self.scopes = org, scopes

    def effective(self, grant: _Grant) -> bool:
        """S9 for an assignment-scoped role: its branch (site manager) or company (partner)."""
        if grant.role in COMPANY_ROLES:
            return self.org.companies.get(grant.company_id, False)  # type: ignore[arg-type]
        return grant.role not in BRANCH_ROLES or self.org.live(grant.branch_id)

    def assigned(self, person: _Person, row: _Assigned) -> bool:
        """S4: the assignment grants (a matching effective role, the project's branch live)."""
        roles = [g for g in person.grants if ROLE_ASSIGNMENT_KIND.get(g.role) == row.kind]
        if not any(self.effective(grant) for grant in roles) or not self.org.live(row.branch_id):
            return False
        strict = any(grant.role in ACTIVE_PROJECTS_ONLY for grant in roles)
        return row.active_project or not strict

    def of_grant(self, person: _Person, grant: _Grant) -> frozenset[object]:
        if grant.role is Role.GROUP_ADMIN:
            return frozenset({_EVERYWHERE})
        if grant.role in _BY_BRANCH:
            return frozenset(self.scopes.get(grant.branch_id, frozenset()))  # type: ignore[arg-type]
        if not self.effective(grant):
            return frozenset()
        kind = ROLE_ASSIGNMENT_KIND[grant.role]
        return frozenset(
            row.project_id
            for row in person.assigned
            if row.kind == kind and self.assigned(person, row)
        )

    def of_person(self, person: _Person) -> frozenset[object]:
        return frozenset().union(*(self.of_grant(person, grant) for grant in person.grants))


def _touched_people(before: _Org, after: _Org, anchors: set[UUID]) -> set[UUID]:
    """Who holds a valid grant whose reach may differ: anchored where S1 changes, a site
    manager's branch or a partner's company that changes state, an assignment on a project
    of a branch that does."""
    flipped = {pk for pk in before.branches if before.live(pk) != after.live(pk)}
    companies = {pk for pk, active in before.companies.items() if after.companies[pk] != active}
    people = Q(user__is_active=True, user__offboarded_at__isnull=True)
    grants = Membership.objects.filter(valid_now(), people).filter(
        Q(role__in=_BY_BRANCH, branch_id__in=anchors)
        | Q(role__in=_BY_ASSIGNMENT, branch_id__in=flipped)
        | Q(role__in=COMPANY_ROLES, company_id__in=companies)
    )
    assigned = ProjectAssignment.objects.filter(valid_now(), people).filter(
        project__branch_id__in=flipped
    )
    return {*grants.values_list("user_id", flat=True), *assigned.values_list("user_id", flat=True)}


def _people(ids: Iterable[UUID]) -> dict[UUID, _Person]:
    """Every valid grant and assignment of these people (S8)."""
    people = {
        pk: _Person(name_of(user), [], [])
        for pk, user in ((user.pk, user) for user in User.objects.filter(pk__in=list(ids)))
    }
    grants = Membership.objects.filter(valid_now(), user_id__in=list(people)).values_list(
        "pk", "user_id", "role", "branch_id", "company_id"
    )
    for pk, user_id, role, branch_id, company_id in grants:
        people[user_id].grants.append(_Grant(pk, Role(role), branch_id, company_id))
    assigned = ProjectAssignment.objects.filter(valid_now(), user_id__in=list(people)).values_list(
        "pk", "user_id", "kind", "project_id", "project__branch_id", "project__status"
    )
    for pk, user_id, kind, project_id, branch_id, status in assigned:
        row = _Assigned(pk, kind, project_id, branch_id, status == ACTIVE_PROJECT_STATUS)
        people[user_id].assigned.append(row)
    return people


type _Scopes = Mapping[UUID, frozenset[UUID]]
type _Moves = tuple[BranchMove, ...]


def _moves(before: _Org, after: _Org, old: _Scopes, new: _Scopes) -> tuple[_Moves, _Moves]:
    """Branches leaving or joining the scope of a managing branch that stays live (leaving)
    or was live (joining); a branch that stops or starts granting is listed on its own."""
    leaving: list[BranchMove] = []
    joining: list[BranchMove] = []
    for anchor, found in before.branches.items():
        if after.live(anchor):
            leaving += [
                BranchMove(before.branches[pk].label, found.label)
                for pk in old[anchor] - new[anchor]
            ]
        if before.live(anchor):
            joining += [
                BranchMove(before.branches[pk].label, found.label)
                for pk in new[anchor] - old[anchor] - {anchor}
            ]
    return tuple(sorted(leaving, key=_order)), tuple(sorted(joining, key=_order))


def _order(move: BranchMove) -> tuple[str, str]:
    return move.manager, move.branch


def _named(people: dict[UUID, _Person], ids: list[UUID]) -> tuple[tuple[UUID, str], ...]:
    ordered = sorted(ids, key=lambda pk: (people[pk].name, str(pk)))
    return tuple((pk, people[pk].name) for pk in ordered[:NAMES_SHOWN])


@dataclass
class _People:
    """What happens to the people a change touches: their grants and assignments whose reach
    changes, and who reaches no branch or project any more (or again)."""

    memberships: int = 0
    assignments: int = 0
    losing: list[UUID] = field(default_factory=list)
    regaining: list[UUID] = field(default_factory=list)

    def add(self, pk: UUID, person: _Person, old: _Reach, new: _Reach) -> None:
        self.memberships += sum(
            old.of_grant(person, g) != new.of_grant(person, g) for g in person.grants
        )
        self.assignments += sum(
            old.assigned(person, row) != new.assigned(person, row) for row in person.assigned
        )
        had, has = old.of_person(person), new.of_person(person)
        if had and not has:
            self.losing.append(pk)
        elif has and not had:
            self.regaining.append(pk)


def _activity_labels(before: _Org, after: _Org) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """The branches that stop granting and those that grant again, by label."""
    flipped = sorted(
        (pk for pk in before.branches if before.live(pk) != after.live(pk)),
        key=lambda pk: before.branches[pk].label,
    )
    stopping = tuple(before.branches[pk].label for pk in flipped if before.live(pk))
    starting = tuple(before.branches[pk].label for pk in flipped if after.live(pk))
    return stopping, starting


def _preview(before: _Org, after: _Org, generation: int) -> ScopePreview:
    old_scopes, new_scopes = before.scopes(), after.scopes()
    anchors = {pk for pk in before.branches if old_scopes[pk] != new_scopes[pk]}
    people = _people(_touched_people(before, after, anchors))
    old, new = _Reach(before, old_scopes), _Reach(after, new_scopes)
    outcome = _People()
    for pk, person in people.items():
        outcome.add(pk, person, old, new)
    leaving, joining = _moves(before, after, old_scopes, new_scopes)
    stopping, starting = _activity_labels(before, after)
    return ScopePreview(
        generation=generation,
        leaving=leaving,
        joining=joining,
        stopping=stopping,
        starting=starting,
        memberships=outcome.memberships,
        assignments=outcome.assignments,
        losing=_named(people, outcome.losing),
        losing_total=len(outcome.losing),
        regaining=_named(people, outcome.regaining),
        regaining_total=len(outcome.regaining),
    )


def _blocked(preview: ScopePreview, branch_ids: list[UUID]) -> ScopePreview:
    """The open documents that make the deactivation of these branches refused (F62)."""
    found = open_documents(branch_ids)
    return replace(preview, blocking=tuple(found[:DOCUMENTS_SHOWN]), blocking_total=len(found))


# --- the three changes ----------------------------------------------------------------------


def current_generation(ctx: AccessContext) -> int:
    """The ``AccessState.org_generation`` a confirmation is given at (``seen_generation``)."""
    if not ctx.has(Capability.ORG_MANAGE):
        raise NotInScope(Capability.ORG_MANAGE)
    return org_generation()


def _start(ctx: AccessContext) -> tuple[int, _Org]:
    """The generation first, then the organization: a change committed in between makes the
    confirmation stale, never the preview older than its generation."""
    return current_generation(ctx), _read_org()


def company_activity(ctx: AccessContext, company: Company, *, active: bool) -> ScopePreview:
    """Deactivating (``active=False``) or reactivating a company."""
    generation, before = _start(ctx)
    preview = _preview(before, before.changed(companies={company.pk: active}), generation)
    if active:
        return preview
    branch_ids = [pk for pk, found in before.branches.items() if found.company_id == company.pk]
    return _blocked(preview, branch_ids)


def branch_activity(ctx: AccessContext, branch: Branch, *, active: bool) -> ScopePreview:
    """Deactivating (``active=False``) or reactivating a branch."""
    generation, before = _start(ctx)
    preview = _preview(before, before.changed(active={branch.pk: active}), generation)
    return preview if active else _blocked(preview, [branch.pk])


def branch_manager(ctx: AccessContext, branch: Branch, manager_id: UUID | None) -> ScopePreview:
    """Giving a branch another managing branch (``None``: no managing branch)."""
    generation, before = _start(ctx)
    if manager_id is not None and manager_id not in before.branches:
        return ScopePreview(generation=generation)
    return _preview(before, before.changed(managers={branch.pk: manager_id}), generation)

"""Property: the engine's scopes equal a reference implementation of S1-S9 (M2.3).

Hypothesis draws random organization graphs: companies and branches (active or not, with
one-level ``managed_by``), projects of any status, users (active or not) with random
memberships and assignments whose validity is open, bounded, expired, future, revoked,
or ends or starts exactly at the database clock. For every user and a set of
capabilities, the branches, projects and locations that ``visible_to`` returns must equal
what ``reference`` derives with plain set logic from the rules as plan §3.5 states them.
"""

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from uuid import UUID

import pytest
from django.db import transaction
from hypothesis import HealthCheck, example, given, settings
from hypothesis import strategies as st

from apps.access.context import AccessContext
from apps.access.roles import (
    BRANCH_ROLES,
    COMPANY_ROLES,
    INSTALLATION_CAPABILITIES,
    ROLE_CAPABILITIES,
    Capability,
    Role,
)
from apps.accounts.models import User
from apps.core.db import db_now
from apps.locations.models import Location
from apps.organizations.models import Branch, Company, Membership
from apps.projects.models import Project, ProjectAssignment

pytestmark = pytest.mark.django_db

HOUR = timedelta(hours=1)
STATUSES = ("ACTIVE", "ON_HOLD", "CLOSED")
KINDS = ("MANAGER", "PARTNER")
# Validity windows relative to the database clock ``now``; True when valid at ``now``
# (and for the few seconds the example runs: ``Now()`` is the statement's time).
VALIDITY: dict[str, bool] = {
    "open": True,
    "bounded": True,
    "starts_now": True,
    "expired": False,
    "future": False,
    "revoked": False,
    "ends_now": False,
}
CAPABILITIES = (
    Capability.ORG_MANAGE,
    Capability.PROJECT_VIEW,
    Capability.PROJECT_MANAGE,
    Capability.CATALOG_VIEW,
    Capability.REQUEST_CREATE,
    Capability.REQUEST_VIEW,
    Capability.REQUEST_REVIEW,
    Capability.PO_VIEW,
    Capability.RECEIPT_POST,
)
TECHNICIAN_LIKE = {Role.TECHNICIAN}


def window(kind: str, now: datetime) -> dict[str, datetime | None]:
    return {
        "open": {"valid_from": now - HOUR, "valid_until": None, "revoked_at": None},
        "bounded": {"valid_from": now - HOUR, "valid_until": now + HOUR, "revoked_at": None},
        "starts_now": {"valid_from": now, "valid_until": None, "revoked_at": None},
        "expired": {"valid_from": now - 2 * HOUR, "valid_until": now - HOUR, "revoked_at": None},
        "future": {"valid_from": now + HOUR, "valid_until": None, "revoked_at": None},
        "revoked": {"valid_from": now - HOUR, "valid_until": None, "revoked_at": now - HOUR / 2},
        "ends_now": {"valid_from": now - HOUR, "valid_until": now, "revoked_at": None},
    }[kind]


@dataclass(frozen=True)
class BranchSpec:
    company: int
    active: bool
    managed_by: int | None


@dataclass(frozen=True)
class ProjectSpec:
    branch: int
    procurement: int
    status: str


@dataclass(frozen=True)
class MembershipSpec:
    role: Role
    target: int  # a branch index for branch roles, a company index for partners
    validity: str


@dataclass(frozen=True)
class AssignmentSpec:
    project: int
    kind: str
    validity: str


@dataclass
class UserSpec:
    active: bool
    memberships: list[MembershipSpec] = field(default_factory=list)
    assignments: list[AssignmentSpec] = field(default_factory=list)


@dataclass
class Graph:
    companies: list[bool]
    branches: list[BranchSpec]
    projects: list[ProjectSpec]
    users: list[UserSpec]


# Valid windows are drawn more often, so that most graphs grant something.
validities = st.sampled_from(["open", "open", "bounded", "starts_now", *sorted(VALIDITY)])
mostly_true = st.sampled_from([True, True, False])


@st.composite
def graphs(draw: st.DrawFn) -> Graph:
    companies = draw(st.lists(mostly_true, min_size=1, max_size=3))
    branches: list[BranchSpec] = []
    for _ in range(draw(st.integers(2, 5))):
        roots = [index for index, spec in enumerate(branches) if spec.managed_by is None]
        managed_by = draw(st.none() | st.sampled_from(roots)) if roots else None
        company = draw(st.integers(0, len(companies) - 1))
        branches.append(BranchSpec(company, draw(mostly_true), managed_by))
    branch_index = st.integers(0, len(branches) - 1)
    projects = draw(
        st.lists(
            st.builds(ProjectSpec, branch_index, branch_index, st.sampled_from(STATUSES)),
            min_size=1,
            max_size=5,
        )
    )
    users: list[UserSpec] = []
    for _ in range(draw(st.integers(1, 4))):
        user = UserSpec(active=draw(mostly_true))
        for _ in range(draw(st.integers(1, 4))):
            role = draw(st.sampled_from(list(Role)))
            if role in BRANCH_ROLES:
                target = draw(branch_index)
            elif role in COMPANY_ROLES:
                target = draw(st.integers(0, len(companies) - 1))
            else:
                target = -1
            user.memberships.append(MembershipSpec(role, target, draw(validities)))
        open_pairs: set[tuple[int, str]] = set()
        for _ in range(draw(st.integers(0, 4))):
            spec = AssignmentSpec(
                draw(st.integers(0, len(projects) - 1)),
                draw(st.sampled_from(KINDS)),
                draw(validities),
            )
            pair = (spec.project, spec.kind)
            if spec.validity != "revoked" and pair in open_pairs:
                continue  # at most one non-revoked row per (project, user, kind)
            if spec.validity != "revoked":
                open_pairs.add(pair)
            user.assignments.append(spec)
        users.append(user)
    return Graph(companies, branches, projects, users)


# Shapes the rules single out, always run in addition to the random ones.
EDGE_CASES = [
    Graph(  # S9: a managed branch that is inactive, or of an inactive company (S1)
        companies=[True, False],
        branches=[BranchSpec(0, True, None), BranchSpec(0, False, 0), BranchSpec(1, True, 0)],
        projects=[ProjectSpec(1, 1, "ACTIVE"), ProjectSpec(2, 2, "ACTIVE")],
        users=[UserSpec(True, [MembershipSpec(Role.MATERIAL_MANAGER, 0, "open")])],
    ),
    Graph(  # Q6: technicians see active projects only; S3 through the procurement branch
        companies=[True],
        branches=[BranchSpec(0, True, None), BranchSpec(0, True, None)],
        projects=[ProjectSpec(1, 0, "ON_HOLD"), ProjectSpec(1, 0, "ACTIVE")],
        users=[UserSpec(True, [MembershipSpec(Role.TECHNICIAN, 0, "open")])],
    ),
    Graph(  # S9 / F50: a partner of an inactive company, alone and with a mixed membership
        companies=[True, False],
        branches=[BranchSpec(0, True, None)],
        projects=[ProjectSpec(0, 0, "ACTIVE"), ProjectSpec(0, 0, "ON_HOLD")],
        users=[
            UserSpec(
                True,
                [MembershipSpec(Role.PARTNER_REQUESTER, 1, "open")],
                [AssignmentSpec(0, "PARTNER", "open")],
            ),
            UserSpec(
                True,
                [
                    MembershipSpec(Role.PARTNER_REQUESTER, 1, "open"),
                    MembershipSpec(Role.TECHNICIAN, 0, "open"),
                ],
                [AssignmentSpec(0, "PARTNER", "open"), AssignmentSpec(1, "PARTNER", "open")],
            ),
            UserSpec(  # S4: partners active projects only, site managers any status
                True,
                [
                    MembershipSpec(Role.PARTNER_REQUESTER, 0, "open"),
                    MembershipSpec(Role.SITE_MANAGER, 0, "starts_now"),
                ],
                [AssignmentSpec(1, "PARTNER", "open"), AssignmentSpec(1, "MANAGER", "bounded")],
            ),
        ],
    ),
    Graph(  # S4 + S9: assigned projects of an inactive branch or of an inactive company
        companies=[True, False],
        branches=[BranchSpec(0, False, None), BranchSpec(0, True, None), BranchSpec(1, True, None)],
        projects=[
            ProjectSpec(0, 1, "ACTIVE"),
            ProjectSpec(2, 1, "ACTIVE"),
            ProjectSpec(1, 1, "ACTIVE"),
        ],
        users=[
            UserSpec(
                True,
                [
                    MembershipSpec(Role.SITE_MANAGER, 1, "open"),
                    MembershipSpec(Role.PARTNER_REQUESTER, 0, "open"),
                ],
                [
                    AssignmentSpec(0, "MANAGER", "open"),
                    AssignmentSpec(1, "MANAGER", "open"),
                    AssignmentSpec(2, "MANAGER", "open"),
                    AssignmentSpec(0, "PARTNER", "open"),
                ],
            )
        ],
    ),
    Graph(  # S8: half-open validity and an inactive user
        companies=[True],
        branches=[BranchSpec(0, True, None)],
        projects=[ProjectSpec(0, 0, "ACTIVE")],
        users=[
            UserSpec(True, [MembershipSpec(Role.GROUP_ADMIN, -1, "ends_now")]),
            UserSpec(False, [MembershipSpec(Role.GROUP_ADMIN, -1, "open")]),
            UserSpec(True, [MembershipSpec(Role.WAREHOUSE_OPERATOR, 0, "starts_now")]),
        ],
    ),
]


@dataclass
class Built:
    companies: list[UUID]
    branches: list[UUID]
    projects: list[UUID]
    users: list[User]
    warehouses: dict[UUID, int]  # location id -> branch index
    worksites: dict[UUID, int]  # location id -> project index


def build(graph: Graph, now: datetime) -> Built:
    companies = [
        Company.objects.create(code=f"C{index}", legal_name=f"C{index}", is_active=active).pk
        for index, active in enumerate(graph.companies)
    ]
    branches: list[UUID] = []
    for index, branch_spec in enumerate(graph.branches):
        manager = branches[branch_spec.managed_by] if branch_spec.managed_by is not None else None
        branch = Branch.objects.create(
            company_id=companies[branch_spec.company],
            code=f"B{index}",
            name=f"B{index}",
            is_active=branch_spec.active,
            managed_by_id=manager,
        )
        branches.append(branch.pk)
    warehouses = {
        Location.objects.create(
            kind=Location.Kind.WAREHOUSE, branch_id=branch_id, name=f"W{index}"
        ).pk: index
        for index, branch_id in enumerate(branches)
    }
    projects: list[UUID] = []
    worksites: dict[UUID, int] = {}
    for index, project_spec in enumerate(graph.projects):
        project = Project.objects.create(
            code=f"P{index}",
            name=f"P{index}",
            branch_id=branches[project_spec.branch],
            procurement_branch_id=branches[project_spec.procurement],
            status=project_spec.status,
        )
        projects.append(project.pk)
        site = Location.objects.create(
            kind=Location.Kind.WORKSITE,
            branch_id=branches[project_spec.branch],
            project=project,
            name=f"S{index}",
        )
        worksites[site.pk] = index
    users: list[User] = []
    for index, user_spec in enumerate(graph.users):
        user = User.objects.create_user(f"u{index}@example.org", is_active=user_spec.active)
        for membership in user_spec.memberships:
            Membership.objects.create(
                user=user,
                role=membership.role,
                branch_id=branches[membership.target] if membership.role in BRANCH_ROLES else None,
                company_id=(
                    companies[membership.target] if membership.role in COMPANY_ROLES else None
                ),
                **window(membership.validity, now),
            )
        for assignment in user_spec.assignments:
            ProjectAssignment.objects.create(
                user=user,
                project_id=projects[assignment.project],
                kind=assignment.kind,
                **window(assignment.validity, now),
            )
        users.append(user)
    return Built(companies, branches, projects, users, warehouses, worksites)


def reference(
    graph: Graph, user: UserSpec, capability: Capability
) -> tuple[set[int], set[int], bool]:
    """The branch and project indices ``capability`` reaches, straight from plan §3.5.

    Returns ``(branches, projects, everything)``.
    """

    def company_active(index: int) -> bool:
        return graph.companies[index]

    def branch_active(index: int) -> bool:  # S9: an inactive company's branches are inactive
        spec = graph.branches[index]
        return spec.active and company_active(spec.company)

    if not user.active:  # S8
        return set(), set(), False
    branches: set[int] = set()
    projects: set[int] = set()
    everything = False
    for membership in user.memberships:
        if not VALIDITY[membership.validity]:  # S8
            continue
        role = membership.role
        if role in BRANCH_ROLES and not branch_active(membership.target):  # S9
            continue
        if role in COMPANY_ROLES and not company_active(membership.target):  # S9, F50
            continue
        if capability not in ROLE_CAPABILITIES[role]:
            continue
        if role is Role.GROUP_ADMIN:  # S2
            everything = True
        elif capability in INSTALLATION_CAPABILITIES:
            continue  # catalog and suppliers: no branch or project rows
        elif role in {Role.MATERIAL_MANAGER, Role.WAREHOUSE_OPERATOR, Role.TECHNICIAN}:
            anchor = membership.target
            scope = {anchor} | {  # S1
                index
                for index, spec in enumerate(graph.branches)
                if spec.managed_by == anchor and branch_active(index)
            }
            branches |= scope
            projects |= {  # S3; technicians: active projects only (Q6)
                index
                for index, spec in enumerate(graph.projects)
                if (spec.branch in scope or spec.procurement in scope)
                and (role not in TECHNICIAN_LIKE or spec.status == "ACTIVE")
            }
        else:  # S4: site managers and partners
            kind = "MANAGER" if role is Role.SITE_MANAGER else "PARTNER"
            projects |= {
                assignment.project
                for assignment in user.assignments
                if assignment.kind == kind
                and VALIDITY[assignment.validity]
                and branch_active(graph.projects[assignment.project].branch)
                and (
                    role is Role.SITE_MANAGER
                    or graph.projects[assignment.project].status == "ACTIVE"
                )
            }
    return branches, projects, everything


def with_edge_cases(test: Callable[..., None]) -> Callable[..., None]:
    for graph in EDGE_CASES:
        test = example(graph=graph)(test)
    return test


@settings(
    max_examples=100,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture, HealthCheck.too_slow],
)
@with_edge_cases
@given(graph=graphs())
def test_resolved_scopes_equal_the_reference(db: None, graph: Graph) -> None:
    with transaction.atomic():
        now = db_now()
        built = build(graph, now)
        for spec, user in zip(graph.users, built.users, strict=True):
            ctx = AccessContext.for_user(User.objects.get(pk=user.pk))
            for capability in CAPABILITIES:
                branch_ix, project_ix, everything = reference(graph, spec, capability)
                if everything:
                    branch_ix = set(range(len(built.branches)))
                    project_ix = set(range(len(built.projects)))
                expected_locations = {
                    pk for pk, index in built.warehouses.items() if index in branch_ix
                } | {pk for pk, index in built.worksites.items() if index in project_ix}
                got_branches = set(
                    Branch.objects.visible_to(ctx, capability).values_list("pk", flat=True)
                )
                got_projects = set(
                    Project.objects.visible_to(ctx, capability).values_list("pk", flat=True)
                )
                got_locations = set(
                    Location.objects.visible_to(ctx, capability).values_list("pk", flat=True)
                )
                label = f"user {spec} / {capability}"
                assert got_branches == {built.branches[i] for i in branch_ix}, label
                assert got_projects == {built.projects[i] for i in project_ix}, label
                assert got_locations == expected_locations, label
        transaction.set_rollback(True)

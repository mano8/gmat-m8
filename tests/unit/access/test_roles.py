"""The capability matrix is the plan's table (plan §3.4), kept as data (apps.access.roles)."""

import pytest

from apps.access.roles import (
    BRANCH_ROLES,
    COMPANY_ROLES,
    INSTALLATION_CAPABILITIES,
    INSTALLATION_ROLES,
    MATRIX_ROLES,
    PRIVILEGED_ROLES,
    ROLE_ASSIGNMENT_KIND,
    ROLE_CAPABILITIES,
    ROLE_SCOPE_BASIS,
    Capability,
    Reach,
    Role,
    ScopeBasis,
)
from apps.projects.models import ProjectAssignment

# Copied from plan §3.4 (columns: GA, MM, WO, SM, TECH, PARTNER).
PLAN_TABLE = """
org.manage          | ● | — | — | — | — | —
project.view        | ● | ● | ● | ○ | ● | ○
project.manage      | ● | ● | — | — | — | —
catalog.view        | ● | ● | ● | ● | ● | ●
catalog.manage      | ● | ● | — | — | — | —
supplier.view       | ● | ● | ● | — | — | —
supplier.manage     | ● | ● | — | — | — | —
request.create      | ○ | ○ | — | ○ | ○ | ○
request.withdraw    | ○ | ○ | — | ○ | ○ | ○
request.view        | ● | ● | ● | ○ | ○ own | ○ own
request.review      | ● | ● | — | ○ | — | —
request.source      | ● | ● | — | — | — | —
request.cancel_any  | ● | ● | — | — | — | —
po.view             | ● | ● | ● | — | — | —
po.manage           | ● | ● | — | — | — | —
price.view          | ● | ● | — | — | — | —
receipt.view        | ● | ● | ● | ○ site | — | —
receipt.post        | ● | ● | ● | ○ site (Q9) | — | —
receipt.void        | ● | ● | — | — | — | —
handover.view       | ● | ● | ● | ○ | — | —
handover.post       | ● | ● | ● | — | — | —
handover.void       | ● | ● | — | — | — | —
audit.view          | ● | ● | — | — | — | —
export.csv          | ● | ● | — | — | — | —
"""
# "○" on one's own drafts is ownership; on anything else it is the assigned projects.
OWN_DRAFTS = {"request.create", "request.withdraw"}


def plan_reach(capability: str, cell: str) -> Reach | None:
    if cell == "—":
        return None
    if cell == "●":
        return Reach.SCOPE
    if cell == "○ own" or capability in OWN_DRAFTS:
        return Reach.OWN
    return Reach.ASSIGNED


def plan_matrix() -> dict[Role, dict[Capability, Reach]]:
    matrix: dict[Role, dict[Capability, Reach]] = {role: {} for role in MATRIX_ROLES}
    for line in PLAN_TABLE.strip().splitlines():
        name, *cells = (part.strip() for part in line.split("|"))
        for role, cell in zip(MATRIX_ROLES, cells, strict=True):
            reach = plan_reach(name, cell)
            if reach is not None:
                matrix[role][Capability(name)] = reach
    return matrix


def test_the_matrix_is_the_plans_table() -> None:
    assert {role: dict(caps) for role, caps in ROLE_CAPABILITIES.items()} == plan_matrix()


def test_every_capability_is_in_the_table() -> None:
    assert {Capability(line.split("|")[0].strip()) for line in PLAN_TABLE.strip().splitlines()} == (
        set(Capability)
    )


def test_the_matrix_is_read_only() -> None:
    with pytest.raises(TypeError):
        ROLE_CAPABILITIES[Role.TECHNICIAN][Capability.PRICE_VIEW] = Reach.SCOPE  # type: ignore[index]


class TestConsistency:
    def test_membership_shapes_partition_the_roles(self) -> None:
        assert set(Role) == INSTALLATION_ROLES | BRANCH_ROLES | COMPANY_ROLES
        assert not (INSTALLATION_ROLES & BRANCH_ROLES)
        assert not (BRANCH_ROLES & COMPANY_ROLES)

    def test_scope_basis_per_role(self) -> None:
        assert set(ROLE_SCOPE_BASIS) == set(Role) == set(MATRIX_ROLES)
        assignment_roles = {
            role for role, basis in ROLE_SCOPE_BASIS.items() if basis is ScopeBasis.ASSIGNMENT
        }
        assert (
            assignment_roles
            == set(ROLE_ASSIGNMENT_KIND)
            == {
                Role.SITE_MANAGER,
                Role.PARTNER_REQUESTER,
            }
        )
        assert set(ROLE_ASSIGNMENT_KIND.values()) == set(ProjectAssignment.Kind.values)

    def test_assigned_reach_only_for_assignment_scoped_roles(self) -> None:
        for role, capabilities in ROLE_CAPABILITIES.items():
            for capability, reach in capabilities.items():
                if reach is Reach.ASSIGNED:
                    assert ROLE_SCOPE_BASIS[role] is ScopeBasis.ASSIGNMENT, (role, capability)

    def test_assignment_scoped_roles_reach_beyond_their_projects_only_for_master_data(
        self,
    ) -> None:
        for role in ROLE_ASSIGNMENT_KIND:
            for capability, reach in ROLE_CAPABILITIES[role].items():
                if reach is Reach.SCOPE:
                    assert capability in INSTALLATION_CAPABILITIES, (role, capability)

    def test_privileged_roles(self) -> None:
        assert {Role.GROUP_ADMIN, Role.MATERIAL_MANAGER} == PRIVILEGED_ROLES

    def test_only_group_admins_manage_the_organization(self) -> None:
        holders = {
            role for role, caps in ROLE_CAPABILITIES.items() if Capability.ORG_MANAGE in caps
        }
        assert holders == {Role.GROUP_ADMIN}

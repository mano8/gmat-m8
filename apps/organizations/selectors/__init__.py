"""Reads for the organization screens (plan §3.4, §3.6).

Branch choices come from ``visible_to``, so a form never offers a branch outside the
user's scope for the capability it serves. Companies, joint-venture members and memberships
are installation-wide data that only ``org.manage`` (group administrators, S2) reads here.

- ``directory``: the choices of the people screens, a person's memberships, the dashboard's
  people per branch;
- ``structure`` (M10.2): companies, joint-venture members and branches, their lists, pages
  and choices;
- ``preview`` (M10.2): what a scope-defining change does to access, before it is made.
"""

from apps.organizations.selectors.directory import (
    branch_labels,
    branches,
    companies,
    membership,
    memberships,
    people_per_branch,
)
from apps.organizations.selectors.preview import (
    BranchMove,
    ScopePreview,
    branch_activity,
    branch_manager,
    company_activity,
    current_generation,
)
from apps.organizations.selectors.structure import (
    BRANCH_SORT_LABELS,
    BRANCH_SORTS,
    COMPANY_SORT_LABELS,
    COMPANY_SORTS,
    DEFAULT_PER_PAGE,
    SECTION_PER_PAGE,
    STATE_ACTIVE,
    STATE_INACTIVE,
    branch,
    branch_list,
    company,
    company_branches,
    company_choices,
    company_list,
    default_warehouse_choices,
    managed_branches,
    manager_candidates,
    people_count,
    project_counts,
    venture_candidates,
    venture_member,
    venture_members,
    warehouses,
)

__all__ = [
    "BRANCH_SORTS",
    "BRANCH_SORT_LABELS",
    "COMPANY_SORTS",
    "COMPANY_SORT_LABELS",
    "DEFAULT_PER_PAGE",
    "SECTION_PER_PAGE",
    "STATE_ACTIVE",
    "STATE_INACTIVE",
    "BranchMove",
    "ScopePreview",
    "branch",
    "branch_activity",
    "branch_labels",
    "branch_list",
    "branch_manager",
    "branches",
    "companies",
    "company",
    "company_activity",
    "company_branches",
    "company_choices",
    "company_list",
    "current_generation",
    "default_warehouse_choices",
    "managed_branches",
    "manager_candidates",
    "membership",
    "memberships",
    "people_count",
    "people_per_branch",
    "project_counts",
    "venture_candidates",
    "venture_member",
    "venture_members",
    "warehouses",
]

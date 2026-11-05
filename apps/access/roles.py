"""Roles, capabilities and the capability matrix (plan §3.4), kept as data.

``ROLE_CAPABILITIES`` is the single source of truth. ``_MATRIX`` below is laid out like the
plan's table, one row per capability and one column per role:

- ``Reach.SCOPE`` (●): every object within the membership's scope;
- ``Reach.ASSIGNED`` (○): only the objects of the projects assigned to the user; used by the
  assignment-scoped roles (site managers, partners), whose scope is those projects;
- ``Reach.OWN`` (○ own): only the objects the user created, within the membership's scope.

``ENGINEER`` (Q30, ADR-0014) ranks below the group administrator and above every other
role: it holds every capability of a material manager in its branch scope. ``request.edit``
(a reviewer's line edits in ``SUBMITTED`` and ``APPROVED``) is used by the services of M7b.3.

``attachment.add`` / ``attachment.remove`` are not capabilities of their own: they follow
the parent document (plan §5, Attachments) and arrive with M6.4.
"""

from enum import Enum, StrEnum
from types import MappingProxyType

from django.db import models
from django.utils.translation import gettext_lazy as _


class Role(models.TextChoices):
    GROUP_ADMIN = "GROUP_ADMIN", _("Group administrator")
    MATERIAL_MANAGER = "MATERIAL_MANAGER", _("Material manager")
    ENGINEER = "ENGINEER", _("Engineer")
    WAREHOUSE_OPERATOR = "WAREHOUSE_OPERATOR", _("Warehouse operator")
    TECHNICIAN = "TECHNICIAN", _("Technician")
    SITE_MANAGER = "SITE_MANAGER", _("Site manager")
    PARTNER_REQUESTER = "PARTNER_REQUESTER", _("Partner requester")


class Capability(StrEnum):
    ORG_MANAGE = "org.manage"
    PROJECT_VIEW = "project.view"
    PROJECT_MANAGE = "project.manage"
    CATALOG_VIEW = "catalog.view"
    CATALOG_MANAGE = "catalog.manage"
    SUPPLIER_VIEW = "supplier.view"
    SUPPLIER_MANAGE = "supplier.manage"
    REQUEST_CREATE = "request.create"
    REQUEST_WITHDRAW = "request.withdraw"
    REQUEST_VIEW = "request.view"
    REQUEST_REVIEW = "request.review"
    REQUEST_EDIT = "request.edit"
    REQUEST_SOURCE = "request.source"
    REQUEST_CANCEL_ANY = "request.cancel_any"
    PO_VIEW = "po.view"
    PO_MANAGE = "po.manage"
    PRICE_VIEW = "price.view"
    RECEIPT_VIEW = "receipt.view"
    RECEIPT_POST = "receipt.post"
    RECEIPT_VOID = "receipt.void"
    HANDOVER_VIEW = "handover.view"
    HANDOVER_POST = "handover.post"
    HANDOVER_VOID = "handover.void"
    AUDIT_VIEW = "audit.view"
    EXPORT_CSV = "export.csv"


class Reach(StrEnum):
    SCOPE = "scope"
    ASSIGNED = "assigned"
    OWN = "own"


class ScopeBasis(Enum):
    """What a membership's scope is made of (plan §3.4, "Effective scope")."""

    INSTALLATION = "installation"  # S2: everything
    BRANCH = "branch"  # S1: the branch and the branches it manages
    ASSIGNMENT = "assignment"  # S4: the projects assigned to the user


# Membership shape per role (plan §3.4): which of ``branch`` / ``company`` is set.
INSTALLATION_ROLES = frozenset({Role.GROUP_ADMIN})
BRANCH_ROLES = frozenset(
    {
        Role.MATERIAL_MANAGER,
        Role.ENGINEER,
        Role.WAREHOUSE_OPERATOR,
        Role.TECHNICIAN,
        Role.SITE_MANAGER,
    }
)
COMPANY_ROLES = frozenset({Role.PARTNER_REQUESTER})

# Roles that require MFA; granting one sets ``User.privilege_elevated_at`` (plan §3.6, §5).
PRIVILEGED_ROLES = frozenset({Role.GROUP_ADMIN, Role.MATERIAL_MANAGER, Role.ENGINEER})

# Not a role's capability: what any signed-in session may do to its own account, whatever its
# memberships say (its own sessions, M8.3). ``authorize_in_txn`` checks the session itself.
OWN_ACCOUNT = "account.own"

# Roles that decide their own requests whatever ``allow_self_approval`` says (Q33,
# ADR-0014): judged by the membership whose grant covers the request (S6). All privileged.
SELF_DECIDING_ROLES = frozenset({Role.GROUP_ADMIN, Role.MATERIAL_MANAGER, Role.ENGINEER})

ROLE_SCOPE_BASIS: MappingProxyType[Role, ScopeBasis] = MappingProxyType(
    {
        Role.GROUP_ADMIN: ScopeBasis.INSTALLATION,
        Role.MATERIAL_MANAGER: ScopeBasis.BRANCH,
        Role.ENGINEER: ScopeBasis.BRANCH,
        Role.WAREHOUSE_OPERATOR: ScopeBasis.BRANCH,
        Role.TECHNICIAN: ScopeBasis.BRANCH,
        Role.SITE_MANAGER: ScopeBasis.ASSIGNMENT,
        Role.PARTNER_REQUESTER: ScopeBasis.ASSIGNMENT,
    }
)

# ``ProjectAssignment.kind`` that builds an assignment-scoped role's scope (S4).
ROLE_ASSIGNMENT_KIND: MappingProxyType[Role, str] = MappingProxyType(
    {Role.SITE_MANAGER: "MANAGER", Role.PARTNER_REQUESTER: "PARTNER"}
)

# Technicians see the active projects of their branches (Q6); partners only active
# assigned projects (S4). Applies to project rows, not to the documents of a project.
ACTIVE_PROJECTS_ONLY = frozenset({Role.TECHNICIAN, Role.PARTNER_REQUESTER})
ACTIVE_PROJECT_STATUS = "ACTIVE"

# Master data shared by the whole installation (plan §4.2: materials, suppliers): a grant
# reaches all of it, never a branch's or project's rows.
INSTALLATION_CAPABILITIES = frozenset(
    {
        Capability.CATALOG_VIEW,
        Capability.CATALOG_MANAGE,
        Capability.SUPPLIER_VIEW,
        Capability.SUPPLIER_MANAGE,
    }
)

# Matrix cells: ● scope, ○ assigned, ○ own, — none.
_SC, _AS, _OW, _NO = Reach.SCOPE, Reach.ASSIGNED, Reach.OWN, None

MATRIX_ROLES = (
    Role.GROUP_ADMIN,
    Role.MATERIAL_MANAGER,
    Role.WAREHOUSE_OPERATOR,
    Role.SITE_MANAGER,
    Role.TECHNICIAN,
    Role.PARTNER_REQUESTER,
    Role.ENGINEER,
)

# fmt: off
_MATRIX: dict[Capability, tuple[Reach | None, ...]] = {
    #                                GA   MM   WO   SM   TECH PART ENG
    Capability.ORG_MANAGE:          (_SC, _NO, _NO, _NO, _NO, _NO, _NO),
    Capability.PROJECT_VIEW:        (_SC, _SC, _SC, _AS, _SC, _AS, _SC),
    Capability.PROJECT_MANAGE:      (_SC, _SC, _NO, _NO, _NO, _NO, _SC),
    Capability.CATALOG_VIEW:        (_SC, _SC, _SC, _SC, _SC, _SC, _SC),
    Capability.CATALOG_MANAGE:      (_SC, _SC, _NO, _NO, _NO, _NO, _SC),
    Capability.SUPPLIER_VIEW:       (_SC, _SC, _SC, _NO, _NO, _NO, _SC),
    Capability.SUPPLIER_MANAGE:     (_SC, _SC, _NO, _NO, _NO, _NO, _SC),
    Capability.REQUEST_CREATE:      (_OW, _OW, _NO, _OW, _OW, _OW, _OW),
    Capability.REQUEST_WITHDRAW:    (_OW, _OW, _NO, _OW, _OW, _OW, _OW),
    Capability.REQUEST_VIEW:        (_SC, _SC, _SC, _AS, _OW, _OW, _SC),
    Capability.REQUEST_REVIEW:      (_SC, _SC, _NO, _AS, _NO, _NO, _SC),
    Capability.REQUEST_EDIT:        (_SC, _SC, _NO, _NO, _NO, _NO, _SC),
    Capability.REQUEST_SOURCE:      (_SC, _SC, _NO, _NO, _NO, _NO, _SC),
    Capability.REQUEST_CANCEL_ANY:  (_SC, _SC, _NO, _NO, _NO, _NO, _SC),
    Capability.PO_VIEW:             (_SC, _SC, _SC, _NO, _NO, _NO, _SC),
    Capability.PO_MANAGE:           (_SC, _SC, _NO, _NO, _NO, _NO, _SC),
    Capability.PRICE_VIEW:          (_SC, _SC, _NO, _NO, _NO, _NO, _SC),
    Capability.RECEIPT_VIEW:        (_SC, _SC, _SC, _AS, _NO, _NO, _SC),
    Capability.RECEIPT_POST:        (_SC, _SC, _SC, _AS, _NO, _NO, _SC),
    Capability.RECEIPT_VOID:        (_SC, _SC, _NO, _NO, _NO, _NO, _SC),
    Capability.HANDOVER_VIEW:       (_SC, _SC, _SC, _AS, _NO, _NO, _SC),
    Capability.HANDOVER_POST:       (_SC, _SC, _SC, _NO, _NO, _NO, _SC),
    Capability.HANDOVER_VOID:       (_SC, _SC, _NO, _NO, _NO, _NO, _SC),
    Capability.AUDIT_VIEW:          (_SC, _SC, _NO, _NO, _NO, _NO, _SC),
    Capability.EXPORT_CSV:          (_SC, _SC, _NO, _NO, _NO, _NO, _SC),
}
# fmt: on

ROLE_CAPABILITIES: MappingProxyType[Role, MappingProxyType[Capability, Reach]] = MappingProxyType(
    {
        role: MappingProxyType(
            {
                capability: reach
                for capability, row in _MATRIX.items()
                if (reach := row[column]) is not None
            }
        )
        for column, role in enumerate(MATRIX_ROLES)
    }
)

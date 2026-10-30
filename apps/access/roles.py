"""Roles and their membership shape (plan §3.4).

M2.3 adds the ``Capability`` enum and the ``ROLE_CAPABILITIES`` matrix next to them.
"""

from django.db import models
from django.utils.translation import gettext_lazy as _


class Role(models.TextChoices):
    GROUP_ADMIN = "GROUP_ADMIN", _("Group administrator")
    MATERIAL_MANAGER = "MATERIAL_MANAGER", _("Material manager")
    WAREHOUSE_OPERATOR = "WAREHOUSE_OPERATOR", _("Warehouse operator")
    TECHNICIAN = "TECHNICIAN", _("Technician")
    SITE_MANAGER = "SITE_MANAGER", _("Site manager")
    PARTNER_REQUESTER = "PARTNER_REQUESTER", _("Partner requester")


# Membership shape per role (plan §3.4): which of ``branch`` / ``company`` is set.
INSTALLATION_ROLES = frozenset({Role.GROUP_ADMIN})
BRANCH_ROLES = frozenset(
    {Role.MATERIAL_MANAGER, Role.WAREHOUSE_OPERATOR, Role.TECHNICIAN, Role.SITE_MANAGER}
)
COMPANY_ROLES = frozenset({Role.PARTNER_REQUESTER})

# Roles that require MFA; granting one sets ``User.privilege_elevated_at`` (plan §3.6, §5).
PRIVILEGED_ROLES = frozenset({Role.GROUP_ADMIN, Role.MATERIAL_MANAGER})

"""Admin for organizations: read-only (ADR-0030).

Companies, joint-venture members and branches are managed on ``/org/structure/``,
memberships on a person's page (``/org/people/``).
"""

from django.contrib import admin

from apps.core.admin import ReadOnlyModelAdmin
from apps.organizations.models import Branch, Company, JointVentureMember, Membership


@admin.register(Company)
class CompanyAdmin(ReadOnlyModelAdmin):
    list_display = ("code", "legal_name", "kind", "tax_id", "is_active")
    list_filter = ("kind", "is_active")
    search_fields = ("code", "legal_name", "tax_id")
    fields = ("code", "legal_name", "tax_id", "kind", "is_active")


@admin.register(JointVentureMember)
class JointVentureMemberAdmin(ReadOnlyModelAdmin):
    list_display = ("venture", "member", "share_pct")
    list_select_related = ("venture", "member")
    fields = ("venture", "member", "share_pct")


@admin.register(Branch)
class BranchAdmin(ReadOnlyModelAdmin):
    list_display = ("code", "name", "company", "city", "managed_by", "is_active")
    list_filter = ("is_active", "company")
    list_select_related = ("company", "managed_by__company")
    search_fields = ("code", "name", "city", "company__code")
    fields = (
        "company",
        "code",
        "name",
        "city",
        "address",
        "managed_by",
        "allow_self_approval",
        "default_warehouse",
        "is_active",
    )


@admin.register(Membership)
class MembershipAdmin(ReadOnlyModelAdmin):
    list_display = ("user", "role", "branch", "company", "valid_from", "valid_until", "revoked_at")
    list_filter = ("role",)
    list_select_related = ("user", "branch__company", "company")
    search_fields = ("user__email",)

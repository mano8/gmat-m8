"""Admin for organizations (plan §5, "Django Admin").

Companies, joint-venture members and branches are master data: editable by group
administrators, saved through the services (generations, audit, deactivation rules).
Memberships are read-only; they change through the in-app screens (M2.6).
"""

from typing import Any, cast

from django.contrib import admin
from django.forms import ModelChoiceField
from django.http import HttpRequest

from apps.access.admin import CapabilityModelAdmin
from apps.access.principals import SessionPrincipal
from apps.access.roles import Capability
from apps.core.admin import ReadOnlyModelAdmin
from apps.locations.models import Location
from apps.organizations import services
from apps.organizations.models import Branch, Company, JointVentureMember, Membership


class MasterDataAdmin(CapabilityModelAdmin):
    """Editable by users who hold ``org.manage``; platform operators alone only view."""

    edit_capability = Capability.ORG_MANAGE


@admin.register(Company)
class CompanyAdmin(MasterDataAdmin):
    list_display = ("code", "legal_name", "kind", "tax_id", "is_active")
    list_filter = ("kind", "is_active")
    search_fields = ("code", "legal_name", "tax_id")

    def get_fields(self, request: HttpRequest, obj: Any = None) -> tuple[str, ...]:
        fields = ("code", "legal_name", "tax_id", "kind")
        return fields if obj is None else (*fields, "is_active")

    def get_readonly_fields(self, request: HttpRequest, obj: Any = None) -> tuple[str, ...]:
        return () if obj is None else ("code",)

    def save_via_service(self, request: HttpRequest, obj: Any, form: Any, change: bool) -> None:
        principal = SessionPrincipal.from_request(request)
        if change:
            saved = services.update_company(principal, obj.pk, **self.form_values(form))
        else:
            fields = ("code", "legal_name", "tax_id", "kind")
            saved = services.create_company(principal, **self.form_values(form, fields))
        self.adopt(obj, saved)


@admin.register(JointVentureMember)
class JointVentureMemberAdmin(MasterDataAdmin):
    list_display = ("venture", "member", "share_pct")
    list_select_related = ("venture", "member")

    def get_fields(self, request: HttpRequest, obj: Any = None) -> tuple[str, ...]:
        return ("venture", "member", "share_pct")

    def get_readonly_fields(self, request: HttpRequest, obj: Any = None) -> tuple[str, ...]:
        return () if obj is None else ("venture", "member")

    def formfield_for_foreignkey(self, db_field: Any, request: HttpRequest, **kwargs: Any) -> Any:
        if db_field.name == "venture":
            kwargs["queryset"] = Company.objects.filter(kind=Company.Kind.JOINT_VENTURE)
        return super().formfield_for_foreignkey(db_field, request, **kwargs)

    def save_via_service(self, request: HttpRequest, obj: Any, form: Any, change: bool) -> None:
        principal = SessionPrincipal.from_request(request)
        if change:
            saved = services.update_joint_venture_member(
                principal, obj.pk, share_pct=form.cleaned_data["share_pct"]
            )
        else:
            values = self.form_values(form, ("venture", "member", "share_pct"))
            saved = services.add_joint_venture_member(principal, **values)
        self.adopt(obj, saved)


@admin.register(Branch)
class BranchAdmin(MasterDataAdmin):
    list_display = ("code", "name", "company", "city", "managed_by", "is_active")
    list_filter = ("is_active", "company")
    list_select_related = ("company", "managed_by__company")
    search_fields = ("code", "name", "city", "company__code")

    ADD_FIELDS = (
        "company",
        "code",
        "name",
        "city",
        "address",
        "managed_by",
        "allow_self_approval",
    )

    def get_fields(self, request: HttpRequest, obj: Any = None) -> tuple[str, ...]:
        if obj is None:
            return self.ADD_FIELDS
        return (*self.ADD_FIELDS, "default_warehouse", "is_active")

    def get_readonly_fields(self, request: HttpRequest, obj: Any = None) -> tuple[str, ...]:
        return () if obj is None else ("company", "code")

    def get_form(
        self, request: HttpRequest, obj: Any = None, change: bool = False, **kwargs: Any
    ) -> Any:
        """Offer only branches that can manage, and this branch's active warehouses.

        A view-only user's form has no editable fields, hence the membership tests.
        """
        form = super().get_form(request, obj, change=change, **kwargs)
        fields = form.base_fields
        if "managed_by" in fields:
            managers = Branch.objects.filter(managed_by__isnull=True).select_related("company")
            cast("ModelChoiceField[Any]", fields["managed_by"]).queryset = (
                managers.exclude(pk=obj.pk) if obj is not None else managers
            )
        if "default_warehouse" in fields:
            cast(
                "ModelChoiceField[Any]", fields["default_warehouse"]
            ).queryset = Location.objects.filter(
                branch=obj, kind=Location.Kind.WAREHOUSE, is_active=True
            )
        return form

    def save_via_service(self, request: HttpRequest, obj: Any, form: Any, change: bool) -> None:
        principal = SessionPrincipal.from_request(request)
        if change:
            saved = services.update_branch(principal, obj.pk, **self.form_values(form))
        else:
            saved = services.create_branch(principal, **self.form_values(form, self.ADD_FIELDS))
        self.adopt(obj, saved)


@admin.register(Membership)
class MembershipAdmin(ReadOnlyModelAdmin):
    list_display = ("user", "role", "branch", "company", "valid_from", "valid_until", "revoked_at")
    list_filter = ("role",)
    list_select_related = ("user", "branch__company", "company")
    search_fields = ("user__email",)

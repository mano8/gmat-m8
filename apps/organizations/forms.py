"""Forms of the group-administrator screens (plan §3.4, §5, M10.2). Choices come from selectors.

The services authorize and validate again in their transaction; these forms only shape what is
sent (a code in upper case, an empty tax id as none).
"""

from decimal import Decimal
from typing import Any

from django import forms
from django.utils.translation import gettext_lazy as _

from apps.access.context import AccessContext
from apps.access.roles import BRANCH_ROLES, COMPANY_ROLES, ROLE_ASSIGNMENT_KIND, Capability, Role
from apps.accounts import selectors as account_selectors
from apps.accounts.services import (
    FULL_NAME_MAX_LENGTH,
    MFA_RESET_OPERATION,
    AssignmentGrant,
    MembershipGrant,
)
from apps.core.forms import IdempotentForm, datetime_field
from apps.core.listing import REVOCATION_CHOICES
from apps.notifications.models import Status
from apps.organizations import selectors
from apps.organizations.models import Branch, Company
from apps.organizations.services import NewBranch
from apps.projects import selectors as project_selectors
from apps.projects.forms import BranchChoiceField
from apps.projects.models import ProjectAssignment

STATUS_CHOICES = [("", _("Everyone")), ("active", _("Active")), ("inactive", _("Inactive"))]


class PeopleFilterForm(forms.Form):
    q = forms.CharField(label=_("Search"), required=False, max_length=100)
    role = forms.ChoiceField(
        label=_("Role"), required=False, choices=[("", _("All roles")), *Role.choices]
    )
    branch = BranchChoiceField(
        label=_("Branch"), queryset=None, required=False, empty_label=_("All branches")
    )
    status = forms.ChoiceField(label=_("Status"), required=False, choices=STATUS_CHOICES)

    def __init__(self, *args: Any, ctx: AccessContext, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.fields["branch"].queryset = selectors.branches(  # type: ignore[attr-defined]
            ctx, Capability.ORG_MANAGE
        )


class InvitationFilterForm(forms.Form):
    q = forms.CharField(label=_("Search"), required=False, max_length=100)
    state = forms.ChoiceField(
        label=_("Validity"),
        required=False,
        choices=[
            ("", _("Any")),
            (account_selectors.VALID, _("Valid")),
            (account_selectors.EXPIRED, _("Expired")),
        ],
    )
    delivery = forms.ChoiceField(
        label=_("Email"),
        required=False,
        choices=[
            ("", _("Any")),
            *Status.choices,
            (account_selectors.NO_EMAIL, _("No email sent")),
        ],
    )


class MembershipFilterForm(forms.Form):
    """Narrows the "Roles" section of a person's page (prefix ``memberships``)."""

    state = forms.ChoiceField(label=_("State"), required=False, choices=REVOCATION_CHOICES)


class SharedProjectFilterForm(forms.Form):
    """Narrows the "Projects shared" section of a person's page (prefix ``projects``)."""

    kind = forms.ChoiceField(
        label=_("Kind"), required=False, choices=[("", _("All")), *ProjectAssignment.Kind.choices]
    )
    state = forms.ChoiceField(label=_("State"), required=False, choices=REVOCATION_CHOICES)


class PersonInvitationFilterForm(forms.Form):
    """Narrows the "Invitations" section of a person's page (prefix ``invitations``)."""

    status = forms.ChoiceField(
        label=_("Status"),
        required=False,
        choices=[
            ("", _("All")),
            (account_selectors.PENDING, _("Pending")),
            (account_selectors.ACCEPTED, _("Accepted")),
            (account_selectors.REVOKED, _("Revoked")),
        ],
    )


class CompanyChoiceField(forms.ModelChoiceField):  # type: ignore[type-arg]
    pass


class ProjectChoiceField(forms.ModelChoiceField):  # type: ignore[type-arg]
    def label_from_instance(self, obj: Any) -> str:
        return f"{obj.code} · {obj.name}"


class RoleTargetMixin(forms.Form):
    """A role with its target: a branch for branch roles, the employer for partners."""

    role = forms.ChoiceField(label=_("Role"), choices=Role.choices)
    branch = BranchChoiceField(
        label=_("Branch"), queryset=None, required=False, help_text=_("For branch roles.")
    )
    valid_until = datetime_field(_("Valid until"), help_text=_("Leave empty for no end."))

    def setup(self, ctx: AccessContext) -> None:
        self.fields["branch"].queryset = selectors.branches(ctx, Capability.ORG_MANAGE)  # type: ignore[attr-defined]

    def clean_role_target(self, data: dict[str, Any], company: Any) -> None:
        role = data.get("role")
        if role in BRANCH_ROLES and data.get("branch") is None:
            self.add_error("branch", _("Choose the branch of this role."))
        if role not in BRANCH_ROLES and data.get("branch") is not None:
            self.add_error("branch", _("This role is not on a branch."))
        if role in COMPANY_ROLES and company is None:
            self.add_error(None, _("A partner needs an employer company."))


class MembershipForm(IdempotentForm, RoleTargetMixin):
    """Grant a role to an existing account."""

    operation = "membership.grant"

    company = CompanyChoiceField(
        label=_("Company"),
        queryset=None,
        required=False,
        help_text=_("For partners: the company that employs them."),
    )
    valid_from = datetime_field(_("Valid from"), help_text=_("Leave empty to start now."))

    def __init__(self, *args: Any, ctx: AccessContext, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.setup(ctx)
        self.fields["company"].queryset = selectors.companies(ctx)  # type: ignore[attr-defined]

    def clean(self) -> dict[str, Any]:
        data = super().clean() or {}
        self.clean_role_target(data, data.get("company"))
        if data.get("role") not in COMPANY_ROLES and data.get("company") is not None:
            self.add_error("company", _("Only a partner's role is on a company."))
        start, end = data.get("valid_from"), data.get("valid_until")
        if start and end and end <= start:
            self.add_error("valid_until", _("The end must be after the start."))
        return data

    def grant_kwargs(self) -> dict[str, Any]:
        data = self.cleaned_data
        return {
            "role": data["role"],
            "branch_id": data["branch"].pk if data.get("branch") else None,
            "company_id": data["company"].pk if data.get("company") else None,
            "valid_from": data.get("valid_from"),
            "valid_until": data.get("valid_until"),
            "idempotency_key": data["idempotency_key"],
        }


class EmployerForm(forms.Form):
    company = CompanyChoiceField(label=_("Employer"), queryset=None, required=False)

    def __init__(self, *args: Any, ctx: AccessContext, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.fields["company"].queryset = selectors.companies(ctx)  # type: ignore[attr-defined]


class EmailChangeForm(forms.Form):
    new_email = forms.EmailField(label=_("New email"), max_length=254)


class ConfirmForm(forms.Form):
    """An irreversible action: the administrator ticks the box."""

    confirm = forms.BooleanField(
        label=_("I understand this cannot be undone."),
        error_messages={"required": _("Tick the confirmation to offboard this account.")},
    )


class ReasonForm(forms.Form):
    reason = forms.CharField(label=_("Reason"), required=False, max_length=200)


class RenameForm(forms.Form):
    """A person's full name (M10.4, ADR-0029): required; the service checks it again."""

    full_name = forms.CharField(label=_("Full name"), max_length=FULL_NAME_MAX_LENGTH)


class MfaResetForm(IdempotentForm):
    """Remove a person's lost second factors (M10.4): a reason, and the administrator's word
    that the person's identity was checked outside the application (never a detail of it)."""

    operation = MFA_RESET_OPERATION

    reason = forms.CharField(
        label=_("Reason"),
        max_length=200,
        help_text=_("Kept in the audit trail, e.g. a lost phone and no recovery code."),
    )
    identity_checked = forms.BooleanField(
        label=_("I checked this person's identity outside the application."),
        error_messages={
            "required": _("Check the person's identity outside the application first.")
        },
    )


class InvitationForm(IdempotentForm, RoleTargetMixin):
    """Invite an email address with one role (and a project for site managers and partners)."""

    operation = "invitation.create"

    email = forms.EmailField(label=_("Email"), max_length=254)
    full_name = forms.CharField(label=_("Full name"), max_length=150)
    employer_company = CompanyChoiceField(
        label=_("Employer"),
        queryset=None,
        required=False,
        help_text=_("Required for partners; their role is on this company."),
    )
    project = ProjectChoiceField(
        label=_("Project"),
        queryset=None,
        required=False,
        help_text=_("Site managers and partners: the project shared with them."),
    )
    field_order = ["email", "full_name", "employer_company", "role", "branch", "project"]

    def __init__(self, *args: Any, ctx: AccessContext, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.setup(ctx)
        self.fields["employer_company"].queryset = selectors.companies(ctx)  # type: ignore[attr-defined]
        self.fields["project"].queryset = project_selectors.projects(ctx, status="ACTIVE")  # type: ignore[attr-defined]

    def clean(self) -> dict[str, Any]:
        data = super().clean() or {}
        self.clean_role_target(data, data.get("employer_company"))
        role = data.get("role")
        if data.get("project") is not None and role not in ROLE_ASSIGNMENT_KIND:
            self.add_error(
                "project", _("Only site managers and partners are assigned to projects.")
            )
        return data

    def invitation_kwargs(self) -> dict[str, Any]:
        data = self.cleaned_data
        role = Role(data["role"])
        employer = data.get("employer_company")
        membership = MembershipGrant(
            role=role,
            branch_id=data["branch"].pk if data.get("branch") else None,
            company_id=employer.pk if role in COMPANY_ROLES and employer else None,
            valid_until=data.get("valid_until"),
        )
        assignments = []
        if data.get("project") is not None:
            assignments.append(
                AssignmentGrant(
                    project_id=data["project"].pk,
                    kind=ROLE_ASSIGNMENT_KIND[role],
                    valid_until=data.get("valid_until"),
                )
            )
        return {
            "email": data["email"],
            "full_name": data["full_name"],
            "employer_company_id": employer.pk if employer else None,
            "memberships": [membership],
            "assignments": assignments,
            "idempotency_key": data["idempotency_key"],
        }


# --- companies, joint ventures and branches (M10.2) ------------------------------------------

STATE_CHOICES = [
    ("", _("All")),
    (selectors.STATE_ACTIVE, _("Active")),
    (selectors.STATE_INACTIVE, _("Inactive")),
]


class CompanyFilterForm(forms.Form):
    q = forms.CharField(label=_("Search"), required=False, max_length=100)
    kind = forms.ChoiceField(
        label=_("Kind"), required=False, choices=[("", _("All kinds")), *Company.Kind.choices]
    )
    state = forms.ChoiceField(label=_("Status"), required=False, choices=STATE_CHOICES)


class CompanyForm(forms.Form):
    """A company's data; ``code`` is fixed once it exists (plan §4.2)."""

    legal_name = forms.CharField(label=_("Legal name"), max_length=200)
    tax_id = forms.CharField(label=_("Tax id"), max_length=32, required=False)
    kind = forms.ChoiceField(
        label=_("Kind"),
        choices=Company.Kind.choices,
        help_text=_("A joint venture records its member companies."),
    )

    def clean_tax_id(self) -> str | None:
        return str(self.cleaned_data["tax_id"]).strip() or None

    def values(self) -> dict[str, Any]:
        return {name: self.cleaned_data[name] for name in ("legal_name", "tax_id", "kind")}

    @staticmethod
    def initial_for(company: Company) -> dict[str, Any]:
        return {"legal_name": company.legal_name, "tax_id": company.tax_id, "kind": company.kind}


class CompanyCreateForm(IdempotentForm, CompanyForm):
    operation = "company.create"

    code = forms.CharField(
        label=_("Code"),
        max_length=20,
        help_text=_("Fixed once created: document numbers are built from it."),
    )
    field_order = ["code", "legal_name", "tax_id", "kind"]

    def clean_code(self) -> str:
        return str(self.cleaned_data["code"]).strip().upper()

    def create_kwargs(self) -> dict[str, Any]:
        data = self.cleaned_data
        return {**self.values(), "code": data["code"], "idempotency_key": data["idempotency_key"]}


class ScopeChangeForm(forms.Form):
    """The confirmation of a scope-defining change: the ``org_generation`` its preview was
    read at, which the service compares under its lock (a change since answers 409)."""

    seen_generation = forms.IntegerField(widget=forms.HiddenInput, min_value=0)


def share_field() -> forms.DecimalField:
    return forms.DecimalField(
        label=_("Share (%)"),
        required=False,
        max_digits=5,
        decimal_places=2,
        min_value=Decimal("0.01"),
        max_value=100,
        help_text=_("Optional; informational."),
    )


class VentureMemberForm(IdempotentForm):
    """A member company joining a joint venture."""

    operation = "joint_venture_member.add"

    member = CompanyChoiceField(label=_("Member company"), queryset=None)
    share_pct = share_field()

    def __init__(self, *args: Any, ctx: AccessContext, venture: Company, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.fields["member"].queryset = selectors.venture_candidates(ctx, venture)  # type: ignore[attr-defined]


class VentureShareForm(forms.Form):
    share_pct = share_field()


class BranchFilterForm(forms.Form):
    q = forms.CharField(label=_("Search"), required=False, max_length=100)
    company = CompanyChoiceField(
        label=_("Company"), queryset=None, required=False, empty_label=_("All companies")
    )
    state = forms.ChoiceField(label=_("Status"), required=False, choices=STATE_CHOICES)

    def __init__(self, *args: Any, ctx: AccessContext, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.fields["company"].queryset = selectors.company_choices(ctx)  # type: ignore[attr-defined]


class ManagerChoiceField(BranchChoiceField):
    """A managing branch; an inactive one says so (it reaches nothing while inactive)."""

    def label_from_instance(self, obj: Any) -> str:
        label = super().label_from_instance(obj)
        live = obj.is_active and obj.company.is_active
        return label if live else _("%(branch)s (inactive)") % {"branch": label}


class WarehouseChoiceField(forms.ModelChoiceField):  # type: ignore[type-arg]
    def label_from_instance(self, obj: Any) -> str:
        return str(obj.name)


class BranchForm(forms.Form):
    """A branch's data and its default warehouse; ``company`` and ``code`` are fixed."""

    name = forms.CharField(label=_("Name"), max_length=200)
    city = forms.CharField(label=_("City"), max_length=100, required=False)
    address = forms.CharField(
        label=_("Address"), required=False, widget=forms.Textarea(attrs={"rows": 2})
    )
    default_warehouse = WarehouseChoiceField(
        label=_("Default warehouse"),
        queryset=None,
        required=False,
        empty_label=_("None"),
        help_text=_("Where goods are received by default; an active warehouse of the branch."),
    )

    def __init__(self, *args: Any, ctx: AccessContext, branch: Branch, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.fields["default_warehouse"].queryset = selectors.default_warehouse_choices(  # type: ignore[attr-defined]
            ctx, branch
        )

    def values(self) -> dict[str, Any]:
        data = self.cleaned_data
        warehouse = data["default_warehouse"]
        return {
            "name": data["name"],
            "city": data["city"],
            "address": data["address"],
            "default_warehouse_id": warehouse.pk if warehouse else None,
        }

    @staticmethod
    def initial_for(branch: Branch) -> dict[str, Any]:
        return {
            "name": branch.name,
            "city": branch.city,
            "address": branch.address,
            "default_warehouse": branch.default_warehouse_id,
        }


class BranchCreateForm(IdempotentForm):
    """A new branch of an active company; its warehouses come after it (Q3)."""

    operation = "branch.create"

    company = CompanyChoiceField(label=_("Company"), queryset=None)
    code = forms.CharField(
        label=_("Code"),
        max_length=20,
        help_text=_("Unique in the company and fixed once created."),
    )
    name = forms.CharField(label=_("Name"), max_length=200)
    city = forms.CharField(label=_("City"), max_length=100, required=False)
    address = forms.CharField(
        label=_("Address"), required=False, widget=forms.Textarea(attrs={"rows": 2})
    )
    managed_by = ManagerChoiceField(
        label=_("Managing branch"),
        queryset=None,
        required=False,
        empty_label=_("None: it manages itself"),
        help_text=_("Who works for the managing branch also reaches this one and its projects."),
    )
    allow_self_approval = forms.BooleanField(
        label=_("Allow self-approval"),
        required=False,
        help_text=_("Lets a requester decide their own requests bought by this branch."),
    )

    def __init__(self, *args: Any, ctx: AccessContext, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.fields["company"].queryset = selectors.companies(ctx)  # type: ignore[attr-defined]
        self.fields["managed_by"].queryset = selectors.manager_candidates(ctx)  # type: ignore[attr-defined]

    def clean_code(self) -> str:
        return str(self.cleaned_data["code"]).strip().upper()

    def new_branch(self) -> NewBranch:
        data = self.cleaned_data
        manager = data["managed_by"]
        return NewBranch(
            company_id=data["company"].pk,
            code=data["code"],
            name=data["name"],
            city=data["city"],
            address=data["address"],
            managed_by_id=manager.pk if manager else None,
            allow_self_approval=data["allow_self_approval"],
        )


MANAGER_PREVIEW = "manager-preview"


class BranchManagerForm(ScopeChangeForm):
    """The managing branch of a branch. Choosing one asks for the preview of the change
    (``preview_url``, htmx) into the dialog's ``MANAGER_PREVIEW`` element."""

    managed_by = ManagerChoiceField(
        label=_("Managing branch"),
        queryset=None,
        required=False,
        empty_label=_("None: it manages itself"),
    )

    def __init__(
        self, *args: Any, ctx: AccessContext, branch: Branch, preview_url: str, **kwargs: Any
    ) -> None:
        super().__init__(*args, **kwargs)
        field = self.fields["managed_by"]
        field.queryset = selectors.manager_candidates(ctx, branch)  # type: ignore[attr-defined]
        field.widget.attrs.update(
            {
                "hx-get": preview_url,
                "hx-trigger": "change",
                "hx-target": f"#{MANAGER_PREVIEW}",
                "hx-swap": "innerHTML",
                "hx-sync": "this:replace",
            }
        )

    def manager_id(self) -> Any:
        manager = self.cleaned_data["managed_by"]
        return manager.pk if manager else None


class SelfApprovalForm(forms.Form):
    """Turn self-approval on or off: the value to set, never a toggle (a double submit sets
    the same value again)."""

    allow = forms.ChoiceField(choices=[("on", "on"), ("off", "off")], widget=forms.HiddenInput)

    def allowed(self) -> bool:
        return bool(self.cleaned_data["allow"] == "on")


class StateFilterForm(forms.Form):
    """Narrows a section of companies' or branches' rows by state (a page with sections)."""

    state = forms.ChoiceField(label=_("Status"), required=False, choices=STATE_CHOICES)

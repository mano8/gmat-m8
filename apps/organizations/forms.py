"""Forms of the group-administrator screens (plan §3.4, §5). Choices come from selectors."""

from typing import Any

from django import forms
from django.utils.translation import gettext_lazy as _

from apps.access.context import AccessContext
from apps.access.roles import BRANCH_ROLES, COMPANY_ROLES, ROLE_ASSIGNMENT_KIND, Capability, Role
from apps.accounts.services import AssignmentGrant, MembershipGrant
from apps.core.forms import IdempotentForm, datetime_field
from apps.organizations import selectors
from apps.projects import selectors as project_selectors
from apps.projects.forms import BranchChoiceField

STATUS_CHOICES = [("", _("Everyone")), ("active", _("Active")), ("inactive", _("Inactive"))]


class PeopleFilterForm(forms.Form):
    q = forms.CharField(label=_("Search"), required=False, max_length=100)
    status = forms.ChoiceField(label=_("Status"), required=False, choices=STATUS_CHOICES)


class InvitationFilterForm(forms.Form):
    q = forms.CharField(label=_("Search"), required=False, max_length=100)


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

    confirm = forms.BooleanField(label=_("I understand this cannot be undone."))


class ReasonForm(forms.Form):
    reason = forms.CharField(label=_("Reason"), required=False, max_length=200)


class InvitationForm(IdempotentForm, RoleTargetMixin):
    """Invite an email address with one role (and a project for site managers and partners)."""

    operation = "invitation.create"

    email = forms.EmailField(label=_("Email"), max_length=254)
    full_name = forms.CharField(label=_("Full name"), max_length=150, required=False)
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

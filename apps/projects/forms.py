"""Project and assignment forms. Every choice comes from a selector (plan §3.6: no IDOR
through a ``<select>``); the services authorize again in their transaction."""

from typing import Any

from django import forms
from django.utils.translation import gettext_lazy as _

from apps.access.context import AccessContext
from apps.access.roles import Capability
from apps.core.forms import IdempotentForm, datetime_field
from apps.organizations import selectors as org_selectors
from apps.organizations.models import CODE_PATTERN
from apps.projects import selectors
from apps.projects.models import Project, ProjectAssignment

STATUS_CHOICES = [("", _("All statuses")), *Project.Status.choices]


class ProjectFilterForm(forms.Form):
    q = forms.CharField(label=_("Search"), required=False, max_length=100)
    status = forms.ChoiceField(label=_("Status"), required=False, choices=STATUS_CHOICES)


class BranchChoiceField(forms.ModelChoiceField):  # type: ignore[type-arg]
    def label_from_instance(self, obj: Any) -> str:
        return f"{obj.company.code}-{obj.code} · {obj.name}"


def _date(label: Any) -> forms.DateField:
    return forms.DateField(
        label=label, required=False, widget=forms.DateInput(attrs={"type": "date"})
    )


class ProjectForm(forms.Form):
    """Edit: everything but the fixed ``code``. ``ProjectCreateForm`` adds it."""

    name = forms.CharField(label=_("Name"), max_length=200)
    client_name = forms.CharField(label=_("Client"), max_length=200, required=False)
    address = forms.CharField(
        label=_("Address"), required=False, widget=forms.Textarea(attrs={"rows": 2})
    )
    branch = BranchChoiceField(
        label=_("Branch"), queryset=None, help_text=_("Executes and bills the project.")
    )
    procurement_branch = BranchChoiceField(
        label=_("Procurement branch"),
        queryset=None,
        required=False,
        help_text=_("Buys for the project. Empty: the branch that manages it, or itself."),
    )
    status = forms.ChoiceField(label=_("Status"), choices=Project.Status.choices)
    start_date = _date(_("Start date"))
    end_date = _date(_("End date"))

    def __init__(self, *args: Any, ctx: AccessContext, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        choices = org_selectors.branches(ctx, Capability.PROJECT_MANAGE)
        self.fields["branch"].queryset = choices  # type: ignore[attr-defined]
        self.fields["procurement_branch"].queryset = choices  # type: ignore[attr-defined]

    def clean(self) -> dict[str, Any]:
        data = super().clean() or {}
        start, end = data.get("start_date"), data.get("end_date")
        if start and end and end < start:
            self.add_error("end_date", _("The end date cannot be before the start date."))
        return data

    def changes(self) -> dict[str, Any]:
        data = self.cleaned_data
        changes = {
            name: data[name]
            for name in ("name", "client_name", "address", "status", "start_date", "end_date")
        }
        changes["branch_id"] = data["branch"].pk
        if data.get("procurement_branch") is not None:
            changes["procurement_branch_id"] = data["procurement_branch"].pk
        return changes

    @staticmethod
    def initial_for(project: Project) -> dict[str, Any]:
        return {
            "name": project.name,
            "client_name": project.client_name,
            "address": project.address,
            "branch": project.branch_id,
            "procurement_branch": project.procurement_branch_id,
            "status": project.status,
            "start_date": project.start_date,
            "end_date": project.end_date,
        }


class ProjectCreateForm(IdempotentForm, ProjectForm):
    operation = "project.create"

    code = forms.RegexField(
        label=_("Code"),
        max_length=30,
        regex=CODE_PATTERN,
        help_text=_("Upper-case letters, digits, '-' and '_'. It cannot change later."),
    )
    field_order = ["code", "name"]

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        del self.fields["status"]  # new projects start ACTIVE

    def create_kwargs(self) -> dict[str, Any]:
        data = self.cleaned_data
        procurement = data.get("procurement_branch")
        return {
            "code": data["code"],
            "name": data["name"],
            "branch_id": data["branch"].pk,
            "procurement_branch_id": procurement.pk if procurement is not None else None,
            "client_name": data["client_name"],
            "address": data["address"],
            "start_date": data["start_date"],
            "end_date": data["end_date"],
            "idempotency_key": data["idempotency_key"],
        }


class UserChoiceField(forms.ModelChoiceField):  # type: ignore[type-arg]
    def label_from_instance(self, obj: Any) -> str:
        return f"{obj.full_name} <{obj.email}>" if obj.full_name else obj.email


class AssignmentForm(IdempotentForm):
    """Share the project with a site manager or a partner (S4)."""

    operation = "project_assignment.grant"

    kind = forms.ChoiceField(label=_("As"), choices=ProjectAssignment.Kind.choices)
    user = UserChoiceField(label=_("Person"), queryset=None)
    valid_until = datetime_field(_("Valid until"), help_text=_("Leave empty for no end."))

    def __init__(self, *args: Any, ctx: AccessContext, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.ctx = ctx
        self.fields["user"].queryset = selectors.assignment_candidates(ctx)  # type: ignore[attr-defined]

    def clean(self) -> dict[str, Any]:
        data = super().clean() or {}
        user, kind = data.get("user"), data.get("kind")
        if (
            user is not None
            and kind
            and not selectors.assignment_candidates(self.ctx, kind).filter(pk=user.pk).exists()
        ):
            self.add_error("user", _("This person does not hold the matching role."))
        return data

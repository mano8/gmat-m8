"""Project screens (plan §3.3, M2.6): list, detail, create, edit and assignments.

Reads go through ``apps.projects.selectors``; writes through ``apps.projects.services`` from
POST-only views (``@mutating``), each on its own URL. The order of the checks is S7's: the
capability somewhere (403), the object in scope (404), the capability on the object (403).
The services authorize again inside their transaction.
"""

from uuid import UUID

from django.contrib import messages
from django.core.exceptions import ValidationError
from django.forms import Form
from django.http import HttpRequest, HttpResponse
from django.shortcuts import redirect, render
from django.utils.translation import gettext as _
from django.views.decorators.http import require_safe

from apps.access.middleware import access_for
from apps.access.mixins import mutating, require, require_capability
from apps.access.roles import Capability
from apps.core.dialogs import lazy
from apps.core.forms import ValidityForm, add_service_errors
from apps.core.http import is_htmx
from apps.core.idempotency import IdempotencyError
from apps.core.listing import ListScope, paginate, render_list
from apps.core.locking import RetryExhausted
from apps.core.responses import conflict, form_again, go, idempotency_failure, invalid, perform
from apps.projects import selectors, services
from apps.projects.forms import (
    AssignmentFilterForm,
    AssignmentForm,
    ProjectCreateForm,
    ProjectFilterForm,
    ProjectForm,
)
from apps.projects.models import Project, ProjectAssignment
from apps.projects.row_dialogs import (
    assignment_revoke_dialog,
    assignment_validity_dialog,
    project_url,
    share_dialog,
)

VIEW, MANAGE = Capability.PROJECT_VIEW, Capability.PROJECT_MANAGE
ASSIGNMENTS_SCOPE = ListScope(prefix="assignments_", anchor="assignments")


@require_safe
@require_capability(VIEW)
def project_list(request: HttpRequest) -> HttpResponse:
    ctx = access_for(request)
    filters = ProjectFilterForm(request.GET or None, ctx=ctx)
    q = status = ""
    branch = None
    if filters.is_valid():
        q, status = filters.cleaned_data["q"], filters.cleaned_data["status"]
        branch = filters.cleaned_data["branch"]
    listing = paginate(
        request,
        selectors.projects(ctx, q=q, status=status, branch_id=branch.pk if branch else None),
        default_per_page=selectors.DEFAULT_PER_PAGE,
        sorts=selectors.PROJECT_SORTS,
        default_sort="code",
        sort_labels=selectors.PROJECT_SORT_LABELS,
    )
    return render_list(request, "projects/list.html", {"filters": filters, "listing": listing})


def _manageable(request: HttpRequest, project_id: UUID) -> Project:
    """The project, if visible (404) and manageable (403) by the request's user."""
    project = selectors.project(access_for(request), project_id)
    require(request, MANAGE, project, read=VIEW)
    return project


def _detail(
    request: HttpRequest,
    project: Project,
    *,
    assignment_form: AssignmentForm | None = None,
    status: int = 200,
) -> HttpResponse:
    ctx = access_for(request)
    manageable = selectors.manageable(ctx, project)
    filters = AssignmentFilterForm(request.GET or None, prefix=ASSIGNMENTS_SCOPE.anchor)
    data = filters.cleaned_data if filters.is_valid() else {}
    listing = paginate(
        request,
        selectors.assignments(ctx, project, kind=data.get("kind", ""), state=data.get("state", "")),
        default_per_page=selectors.SECTION_PER_PAGE,
        scope=ASSIGNMENTS_SCOPE,
    )
    context = {
        "project": project,
        "manageable": manageable,
        "assignment_filters": filters,
        "assignments": listing,
        "share_dialog": share_dialog(
            project,
            assignment_form or AssignmentForm(ctx=ctx, actor_id=ctx.user_id, auto_id=SHARE_IDS),
        )
        if manageable
        else None,
    }
    return render(request, "projects/detail.html", context, status=status)


@require_safe
@require_capability(VIEW)
def project_detail(request: HttpRequest, project_id: UUID) -> HttpResponse:
    return _detail(request, selectors.project(access_for(request), project_id))


@require_safe
@require_capability(MANAGE)
def project_new(request: HttpRequest) -> HttpResponse:
    ctx = access_for(request)
    form = ProjectCreateForm(ctx=ctx, actor_id=ctx.user_id)
    return render(request, "projects/form.html", {"form": form, "project": None})


@mutating(MANAGE)
def project_create(request: HttpRequest) -> HttpResponse:
    ctx = access_for(request)
    form = ProjectCreateForm(request.POST, ctx=ctx)
    if form.is_valid():
        try:
            project = services.create_project(
                ctx.principal,
                form.new_project(),
                idempotency_key=form.cleaned_data["idempotency_key"],
            )
        except ValidationError as exc:
            add_service_errors(form, exc)
        except IdempotencyError as exc:
            return idempotency_failure(request, exc)
        except RetryExhausted:
            return conflict(request)
        else:
            messages.success(request, _("Project %(code)s created.") % {"code": project.code})
            return redirect("projects:detail", project_id=project.pk)
    return render(request, "projects/form.html", {"form": form, "project": None})


@require_safe
@require_capability(MANAGE)
def project_edit(request: HttpRequest, project_id: UUID) -> HttpResponse:
    ctx = access_for(request)
    project = _manageable(request, project_id)
    form = ProjectForm(initial=ProjectForm.initial_for(project), ctx=ctx)
    return render(request, "projects/form.html", {"form": form, "project": project})


@mutating(MANAGE)
def project_update(request: HttpRequest, project_id: UUID) -> HttpResponse:
    ctx = access_for(request)
    project = _manageable(request, project_id)
    form = ProjectForm(request.POST, ctx=ctx)
    if form.is_valid():
        try:
            services.update_project(ctx.principal, project.pk, **form.changes())
        except ValidationError as exc:
            add_service_errors(form, exc)
        except RetryExhausted:
            return conflict(request)
        else:
            messages.success(request, _("Project %(code)s saved.") % {"code": project.code})
            return redirect("projects:detail", project_id=project.pk)
    return render(request, "projects/form.html", {"form": form, "project": project})


SHARE_IDS = "id_share_%s"
VALIDITY_IDS = "id_validity_%s"


@mutating(MANAGE)
def assignment_create(request: HttpRequest, project_id: UUID) -> HttpResponse:
    ctx = access_for(request)
    project = _manageable(request, project_id)
    form = AssignmentForm(request.POST, ctx=ctx, auto_id=SHARE_IDS)
    if form.is_valid():
        data = form.cleaned_data
        try:
            services.assign_to_project(
                ctx.principal,
                project_id=project.pk,
                user_id=data["user"].pk,
                kind=data["kind"],
                valid_until=data["valid_until"],
                idempotency_key=data["idempotency_key"],
            )
        except ValidationError as exc:
            add_service_errors(form, exc)
        except IdempotencyError as exc:
            return idempotency_failure(request, exc)
        except RetryExhausted:
            return conflict(request)
        else:
            messages.success(request, _("Project shared."))
            return go(request, "projects:detail", project_id=project.pk)
    if is_htmx(request):
        return form_again(request, share_dialog(project, form).dialog())
    return _detail(request, project, assignment_form=form)


def _assignment(request: HttpRequest, assignment_id: UUID) -> ProjectAssignment:
    """The assignment, if in scope (404) and its project manageable (403)."""
    assignment = selectors.assignment(access_for(request), assignment_id)
    require(request, MANAGE, assignment.project, read=VIEW)
    return assignment


@require_safe
@require_capability(MANAGE)
def assignment_validity_form(request: HttpRequest, assignment_id: UUID) -> HttpResponse:
    """The dialog that changes an assignment's validity (its POST is ``assignment_validity``)."""
    assignment = _assignment(request, assignment_id)
    form = ValidityForm(
        initial={"valid_from": assignment.valid_from, "valid_until": assignment.valid_until},
        auto_id=VALIDITY_IDS,
    )
    return lazy(request, assignment_validity_dialog(assignment, form))


@require_safe
@require_capability(MANAGE)
def assignment_revoke_form(request: HttpRequest, assignment_id: UUID) -> HttpResponse:
    """The dialog that confirms revoking an assignment (its POST is ``assignment_revoke``)."""
    assignment = _assignment(request, assignment_id)
    return lazy(request, assignment_revoke_dialog(assignment, Form()))


@mutating(MANAGE)
def assignment_revoke(request: HttpRequest, assignment_id: UUID) -> HttpResponse:
    ctx = access_for(request)
    assignment = _assignment(request, assignment_id)
    form = Form(request.POST)
    form.is_valid()  # no fields: a service refusal can then be shown on it
    return perform(
        request,
        lambda: services.revoke_project_assignment(ctx.principal, assignment.pk),
        _("Access to the project revoked."),
        project_url(assignment.project_id),
        dialog=assignment_revoke_dialog(assignment, form).dialog(),
    )


@mutating(MANAGE)
def assignment_validity(request: HttpRequest, assignment_id: UUID) -> HttpResponse:
    ctx = access_for(request)
    assignment = _assignment(request, assignment_id)
    form = ValidityForm(request.POST, auto_id=VALIDITY_IDS)
    dialog = assignment_validity_dialog(assignment, form).dialog()
    to = project_url(assignment.project_id)
    if not form.is_valid():
        return invalid(request, form, to, dialog=dialog)
    return perform(
        request,
        lambda: services.change_assignment_validity(
            ctx.principal,
            assignment.pk,
            valid_from=form.cleaned_data["valid_from"],
            valid_until=form.cleaned_data["valid_until"],
        ),
        _("Validity saved."),
        to,
        dialog=dialog,
    )

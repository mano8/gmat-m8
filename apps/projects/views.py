"""Project screens (plan §3.3, M2.6): list, detail, create, edit and assignments.

Reads go through ``apps.projects.selectors``; writes through ``apps.projects.services`` from
POST-only views (``@mutating``), each on its own URL. The order of the checks is S7's: the
capability somewhere (403), the object in scope (404), the capability on the object (403).
The services authorize again inside their transaction.
"""

from urllib.parse import urlencode
from uuid import UUID

from django.contrib import messages
from django.core.exceptions import ValidationError
from django.core.paginator import Paginator
from django.http import HttpRequest, HttpResponse
from django.shortcuts import redirect, render
from django.utils.translation import gettext as _
from django.views.decorators.http import require_safe

from apps.access.middleware import access_for
from apps.access.mixins import mutating, require, require_capability
from apps.access.roles import Capability
from apps.core.forms import ValidityForm, add_service_errors, error_text, form_error_text
from apps.core.http import is_htmx
from apps.core.idempotency import IdempotencyError
from apps.core.locking import RetryExhausted
from apps.core.responses import conflict, idempotency_failure
from apps.projects import selectors, services
from apps.projects.forms import AssignmentForm, ProjectCreateForm, ProjectFilterForm, ProjectForm
from apps.projects.models import Project

VIEW, MANAGE = Capability.PROJECT_VIEW, Capability.PROJECT_MANAGE


@require_safe
@require_capability(VIEW)
def project_list(request: HttpRequest) -> HttpResponse:
    ctx = access_for(request)
    filters = ProjectFilterForm(request.GET or None)
    q = status = ""
    if filters.is_valid():
        q, status = filters.cleaned_data["q"], filters.cleaned_data["status"]
    page = Paginator(selectors.projects(ctx, q=q, status=status), selectors.PAGE_SIZE).get_page(
        request.GET.get("page")
    )
    template = "projects/list.html#results" if is_htmx(request) else "projects/list.html"
    return render(
        request,
        template,
        {"filters": filters, "page_obj": page, "extra_query": _query(q, status)},
    )


def _query(q: str, status: str) -> str:
    """The filters, for the pager's links."""
    params = {key: value for key, value in (("q", q), ("status", status)) if value}
    return f"&{urlencode(params)}" if params else ""


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
    context = {
        "project": project,
        "manageable": manageable,
        "assignments": selectors.assignments(ctx, project),
        "assignment_form": assignment_form
        or (AssignmentForm(ctx=ctx, actor_id=ctx.user_id) if manageable else None),
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
            project = services.create_project(ctx.principal, **form.create_kwargs())
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


@mutating(MANAGE)
def assignment_create(request: HttpRequest, project_id: UUID) -> HttpResponse:
    ctx = access_for(request)
    project = _manageable(request, project_id)
    form = AssignmentForm(request.POST, ctx=ctx)
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
            return redirect("projects:detail", project_id=project.pk)
    return _detail(request, project, assignment_form=form)


@mutating(MANAGE)
def assignment_revoke(request: HttpRequest, assignment_id: UUID) -> HttpResponse:
    ctx = access_for(request)
    assignment = selectors.assignment(ctx, assignment_id)
    require(request, MANAGE, assignment.project, read=VIEW)
    try:
        services.revoke_project_assignment(ctx.principal, assignment.pk)
    except ValidationError as exc:
        messages.error(request, error_text(exc))
    except RetryExhausted:
        return conflict(request)
    else:
        messages.success(request, _("Access to the project revoked."))
    return redirect("projects:detail", project_id=assignment.project_id)


@mutating(MANAGE)
def assignment_validity(request: HttpRequest, assignment_id: UUID) -> HttpResponse:
    ctx = access_for(request)
    assignment = selectors.assignment(ctx, assignment_id)
    require(request, MANAGE, assignment.project, read=VIEW)
    form = ValidityForm(request.POST)
    if not form.is_valid():
        messages.error(request, form_error_text(form))
    else:
        try:
            services.change_assignment_validity(
                ctx.principal,
                assignment.pk,
                valid_from=form.cleaned_data["valid_from"],
                valid_until=form.cleaned_data["valid_until"],
            )
        except ValidationError as exc:
            messages.error(request, error_text(exc))
        except RetryExhausted:
            return conflict(request)
        else:
            messages.success(request, _("Validity saved."))
    return redirect("projects:detail", project_id=assignment.project_id)

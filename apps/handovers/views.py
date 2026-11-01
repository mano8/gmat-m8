"""The handover screens (plan §11 M6.3): the list and page of handovers, the form that hands
material of a warehouse over to a project (from a request or from a project) and the void.

Reads go through ``apps.handovers.selectors``; writes through ``apps.handovers.services``
from POST-only views (``@mutating``), each on its own URL, with its GET page on another.
Capabilities: ``handover.view`` for the list and the page (a site manager reads the
handovers of their project), ``handover.post`` for the form, ``handover.void`` for voiding a
warehouse handover. A handover made on site is voided with its goods receipt.

The warehouse's known stock is only a warning (I9): when a line asks for more than the
warehouse is known to hold, the form comes back once with the shortfall and a box to tick.
"""

from typing import Any
from uuid import UUID

from django.contrib import messages
from django.core.exceptions import ValidationError
from django.core.paginator import Paginator
from django.http import HttpRequest, HttpResponse
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils.translation import gettext as _
from django.views.decorators.http import require_safe

from apps.access.exceptions import NotInScope
from apps.access.middleware import access_for
from apps.access.mixins import mutating, require, require_capability
from apps.core.forms import add_service_errors
from apps.core.http import is_htmx
from apps.core.idempotency import IdempotencyError, recorded_result
from apps.core.locking import RetryExhausted
from apps.core.responses import conflict, idempotency_failure, invalid, perform
from apps.handovers import selectors, services
from apps.handovers.forms import Choices, HandoverFilterForm, HandoverForm, RequiredReasonForm
from apps.handovers.models import Handover
from apps.handovers.selectors import POST, VIEW, VOID, Shortfall
from apps.material_requests.models import MaterialRequest
from apps.projects.models import Project


def _uuid(value: str | None) -> UUID | None:
    try:
        return UUID(value or "")
    except ValueError:
        return None


# --- handovers -------------------------------


@require_safe
@require_capability(VIEW)
def handover_list(request: HttpRequest) -> HttpResponse:
    ctx = access_for(request)
    filters = HandoverFilterForm(request.GET or None, projects=selectors.list_projects(ctx))
    data = filters.cleaned_data if filters.is_valid() else {}
    project = data.get("project")
    rows = selectors.handovers(
        ctx,
        status=data.get("status", ""),
        kind=data.get("kind", ""),
        project_id=project.pk if project else None,
        q=data.get("q", ""),
    )
    page = Paginator(rows, 25).get_page(request.GET.get("page"))
    template = "handovers/list.html#results" if is_htmx(request) else "handovers/list.html"
    kept = {key: data[key] for key in ("q", "status", "kind") if data.get(key)}
    extra = "".join(f"&{key}={value}" for key, value in kept.items())
    if project:
        extra += f"&project={project.pk}"
    context = {
        "filters": filters,
        "page_obj": page,
        "can_post": selectors.can_post(ctx),
        "extra_query": extra,
    }
    return render(request, template, context)


@require_safe
@require_capability(VIEW)
def handover_detail(request: HttpRequest, handover_id: UUID) -> HttpResponse:
    ctx = access_for(request)
    found = selectors.handover(ctx, handover_id)
    can_void = selectors.can_void(ctx, found)
    context = {
        "handover": found,
        "lines": selectors.handover_lines(ctx, found),
        "can_void": can_void,
        "void_form": RequiredReasonForm(auto_id="id_void_%s") if can_void else None,
    }
    return render(request, "handovers/detail.html", context)


@mutating(VOID)
def handover_void(request: HttpRequest, handover_id: UUID) -> HttpResponse:
    ctx = access_for(request)
    found = selectors.handover(ctx, handover_id)
    require(request, VOID, found, read=VIEW)
    to = reverse("handovers:detail", args=[found.pk])
    form = RequiredReasonForm(request.POST)
    if not form.is_valid():
        return invalid(request, form, to)
    return perform(
        request,
        lambda: services.void_handover(ctx.principal, found.pk, reason=form.cleaned_data["reason"]),
        _("Handover %(number)s voided.") % {"number": found.number},
        to,
    )


# --- the handover form -------------------------------


def _scope_of(
    request: HttpRequest, project_id: UUID | None, request_id: UUID | None
) -> tuple[Project, MaterialRequest | None] | None:
    """The project (and request) the form is for; ``None`` when none was named."""
    ctx = access_for(request)
    asked: MaterialRequest | None = None
    if request_id is not None:
        asked = selectors.posting_request(ctx, request_id)
        project_id = asked.project_id
    if project_id is None:
        return None
    return selectors.posting_project(ctx, project_id), asked


def _form_for(
    request: HttpRequest, project: Project, asked: MaterialRequest | None, data: Any = None
) -> HandoverForm:
    ctx = access_for(request)
    branch_id = asked.procurement_branch_id if asked else project.procurement_branch_id
    choices = Choices(
        project=project,
        rows=selectors.open_lines(ctx, project, request=asked),
        sources=list(selectors.sources(ctx, branch_id)),
        receivers=selectors.receivers(project),
        request_id=asked.pk if asked else None,
    )
    return HandoverForm(data, choices=choices, actor_id=ctx.user_id)


def _form_page(
    request: HttpRequest,
    project: Project,
    asked: MaterialRequest | None,
    form: HandoverForm,
    warnings: list[Shortfall] | None = None,
    status: int = 200,
) -> HttpResponse:
    context = {
        "project": project,
        "asked": asked,
        "form": form,
        "entries": form.entries(),
        "warnings": warnings or [],
    }
    return render(request, "handovers/new.html", context, status=status)


@require_safe
@require_capability(POST)
def handover_new(request: HttpRequest) -> HttpResponse:
    """The form for a project or a request; without one, the projects to choose from."""
    ctx = access_for(request)
    scope = _scope_of(request, _uuid(request.GET.get("project")), _uuid(request.GET.get("request")))
    if scope is None:
        projects = selectors.posting_projects(ctx)
        return render(request, "handovers/projects.html", {"projects": projects})
    project, asked = scope
    return _form_page(request, project, asked, _form_for(request, project, asked))


def _post_handover(
    request: HttpRequest, project: Project, form: HandoverForm
) -> HttpResponse | None:
    """Post a valid form: the redirect to the handover, or ``None`` when the service
    refused it (its errors are then on the form)."""
    ctx = access_for(request)
    try:
        handover = services.create_handover(
            ctx.principal,
            project.pk,
            source_location_id=form.source_id(),
            lines=form.line_inputs(),
            receiver=form.receiver(),
            idempotency_key=form.cleaned_data["idempotency_key"],
        )
    except ValidationError as exc:
        add_service_errors(form, exc)
        return None
    except IdempotencyError as exc:
        return idempotency_failure(request, exc)
    except RetryExhausted:
        return conflict(request)
    messages.success(request, _("Handover %(number)s posted.") % {"number": handover.number})
    return redirect("handovers:detail", handover_id=handover.pk)


def _already_posted(request: HttpRequest) -> HttpResponse | None:
    """A form that is no longer valid because its key already posted a handover (a double
    tap that handed the last of a line over): go to that handover instead of showing errors."""
    ctx = access_for(request)
    key = request.POST.get("idempotency_key", "")
    if not key:
        return None
    try:
        done = recorded_result(
            key, actor_id=ctx.principal.user_id, operation=HandoverForm.operation
        )
    except IdempotencyError as exc:
        return idempotency_failure(request, exc)
    if done is None or done[0] != Handover._meta.label_lower:
        return None
    try:
        handover = selectors.handover(ctx, done[1])
    except NotInScope:
        return None
    messages.info(
        request, _("Handover %(number)s was already posted.") % {"number": handover.number}
    )
    return redirect("handovers:detail", handover_id=handover.pk)


def _posted_scope(request: HttpRequest) -> tuple[Project, MaterialRequest | None] | HttpResponse:
    """The project (and request) of a post, or the answer when there is none: back to the
    choice, or to the handover this key already made (its first tap handed the last of the
    project over, so the project has nothing left)."""
    try:
        scope = _scope_of(
            request, _uuid(request.POST.get("project")), _uuid(request.POST.get("request"))
        )
    except NotInScope:
        answer = _already_posted(request)
        if answer is None:
            raise
        return answer
    if scope is None:
        messages.error(request, _("Choose a project."))
        return redirect("handovers:new")
    return scope


@mutating(POST)
def handover_create(request: HttpRequest) -> HttpResponse:
    """Post the handover. A double tap carries the same key and returns the same handover;
    a shortfall against the known stock comes back once as a warning (I9)."""
    ctx = access_for(request)
    scope = _posted_scope(request)
    if isinstance(scope, HttpResponse):
        return scope
    project, asked = scope
    form = _form_for(request, project, asked, request.POST)
    if not form.is_valid():
        return _already_posted(request) or _form_page(request, project, asked, form)
    warnings = selectors.stock_warnings(ctx, form.source_id(), form.quantities())
    if warnings and not form.cleaned_data["acknowledge"]:
        return _form_page(request, project, asked, form, warnings)
    answer = _post_handover(request, project, form)
    return answer if answer is not None else _form_page(request, project, asked, form)

"""The audit screens (plan M7.3, ``audit.view``): the scoped event list with filters and the
history of one object. Both only read, through ``apps.audit.selectors``."""

from uuid import UUID

from django.core.paginator import Paginator
from django.http import HttpRequest, HttpResponse
from django.shortcuts import render
from django.views.decorators.http import require_safe

from apps.access.middleware import access_for
from apps.access.mixins import require_capability
from apps.audit import selectors
from apps.audit.forms import AuditFilterForm
from apps.core.http import is_htmx


def _extra_query(**params: str) -> str:
    return "".join(f"&{key}={value}" for key, value in params.items() if value)


@require_safe
@require_capability(selectors.VIEW)
def event_list(request: HttpRequest) -> HttpResponse:
    ctx = access_for(request)
    filters = AuditFilterForm(request.GET or None, object_types=selectors.object_types(ctx))
    data = filters.cleaned_data if filters.is_valid() else {}
    narrow = selectors.EventFilter(
        q=data.get("q", ""),
        object_type=data.get("object_type", ""),
        since=data.get("since"),
        until=data.get("until"),
    )
    page = Paginator(selectors.events(ctx, narrow), selectors.PAGE_SIZE).get_page(
        request.GET.get("page")
    )
    template = "audit/list.html#results" if is_htmx(request) else "audit/list.html"
    context = {
        "filters": filters,
        "page_obj": page,
        "extra_query": _extra_query(
            q=data.get("q", ""),
            object_type=data.get("object_type", ""),
            since=request.GET.get("since", "") if data.get("since") else "",
            until=request.GET.get("until", "") if data.get("until") else "",
        ),
    }
    return render(request, template, context)


@require_safe
@require_capability(selectors.VIEW)
def object_history(request: HttpRequest, object_type: str, object_id: UUID) -> HttpResponse:
    rows = selectors.object_history(access_for(request), object_type, object_id)
    context = {"object_type": object_type, "object_id": object_id, "events": rows}
    return render(request, "audit/object.html", context)

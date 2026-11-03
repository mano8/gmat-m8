"""The audit screens (plan M7.3, ``audit.view``): the scoped event list with filters and the
history of one object. Both only read, through ``apps.audit.selectors``."""

from uuid import UUID

from django.http import HttpRequest, HttpResponse
from django.views.decorators.http import require_safe

from apps.access.middleware import access_for
from apps.access.mixins import require_capability
from apps.audit import selectors
from apps.audit.forms import AuditFilterForm, HistoryFilterForm
from apps.core.listing import paginate, render_list


@require_safe
@require_capability(selectors.VIEW)
def event_list(request: HttpRequest) -> HttpResponse:
    ctx = access_for(request)
    filters = AuditFilterForm(request.GET or None, object_types=selectors.object_types(ctx))
    data = filters.cleaned_data if filters.is_valid() else {}
    narrow = selectors.EventFilter(
        q=data.get("q", ""),
        actor=data.get("actor", ""),
        object_type=data.get("object_type", ""),
        since=data.get("since"),
        until=data.get("until"),
    )
    listing = paginate(
        request,
        selectors.events(ctx, narrow),
        default_per_page=selectors.DEFAULT_PER_PAGE,
        sorts=selectors.EVENT_SORTS,
        default_sort="-time",
        sort_labels=selectors.EVENT_SORT_LABELS,
    )
    return render_list(request, "audit/list.html", {"filters": filters, "listing": listing})


@require_safe
@require_capability(selectors.VIEW)
def object_history(request: HttpRequest, object_type: str, object_id: UUID) -> HttpResponse:
    ctx = access_for(request)
    filters = HistoryFilterForm(request.GET or None)
    data = filters.cleaned_data if filters.is_valid() else {}
    narrow = selectors.EventFilter(
        q=data.get("q", ""), since=data.get("since"), until=data.get("until")
    )
    listing = paginate(
        request,
        selectors.object_history(ctx, object_type, object_id, narrow),
        default_per_page=selectors.DEFAULT_PER_PAGE,
        sorts=selectors.EVENT_SORTS,
        default_sort="time",
        sort_labels=selectors.EVENT_SORT_LABELS,
    )
    context = {
        "object_type": object_type,
        "object_id": object_id,
        "filters": filters,
        "listing": listing,
    }
    return render_list(request, "audit/object.html", context)

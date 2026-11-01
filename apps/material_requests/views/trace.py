"""The traceability screens (plan §11 M6.5): the ledger of a request, a project's materials.

Both are read-only and reach whoever holds ``request.view`` on the request or project; the
numbers come from ``selectors`` (the request's ledger, ``purchasing.selectors`` for the
trims, which only who follows the request up sees).
"""

from uuid import UUID

from django.http import HttpRequest, HttpResponse
from django.shortcuts import render
from django.views.decorators.http import require_safe

from apps.access.middleware import access_for
from apps.access.mixins import require_capability
from apps.access.roles import Capability
from apps.material_requests import selectors
from apps.purchasing import selectors as purchasing_selectors

VIEW = Capability.REQUEST_VIEW


@require_safe
@require_capability(VIEW)
def request_ledger(request: HttpRequest, request_id: UUID) -> HttpResponse:
    ctx = access_for(request)
    found = selectors.request(ctx, request_id)
    trims = purchasing_selectors.request_trims(ctx, found)
    entries = selectors.lines(ctx, [found])[found.pk]
    context = {
        "request_obj": found,
        "entries": [(entry, trims.get(entry.line.line_no, [])) for entry in entries],
        "has_trims": bool(trims),
    }
    return render(request, "material_requests/ledger.html", context)


@require_safe
@require_capability(VIEW)
def project_materials(request: HttpRequest, project_id: UUID) -> HttpResponse:
    ctx = access_for(request)
    found = selectors.project_for_overview(ctx, project_id)
    context = {"project": found, "materials": selectors.project_materials(ctx, found)}
    return render(request, "material_requests/project_materials.html", context)

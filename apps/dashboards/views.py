"""The home page, its key figures and its lazily loaded panels (plans M7.2, M8b.5).

All read-only (GET and HEAD). The home page lists the panels the user's capabilities reach;
a panel's fragment needs every capability the panel reads with (403 otherwise, 404 for an
unknown key), and its rows and counts come from selectors that apply each capability's
scope. The period (``?weeks=``) is in the URL only; a value outside the allowlist falls
back to the default.
"""

from django.contrib.auth.decorators import login_required
from django.http import Http404, HttpRequest, HttpResponse
from django.shortcuts import render
from django.views.decorators.http import require_safe

from apps.access.middleware import access_for
from apps.access.mixins import require
from apps.dashboards import panels, period, tiles
from apps.dashboards.chart import ChartPanel


def _greeting_name(request: HttpRequest) -> str:
    user = request.user
    name = getattr(user, "full_name", "") or ""
    return name.split()[0] if name.strip() else getattr(user, "email", "")


@require_safe
@login_required
def home(request: HttpRequest) -> HttpResponse:
    """Landing page after login: the key figures, the charts and the task panels, each a
    placeholder that loads its own fragment."""
    ctx = access_for(request)
    found = panels.visible(ctx)
    charts = [panel for panel in found if isinstance(panel, ChartPanel)]
    shown = period.period_of(request)
    context = {
        "charts": charts,
        "panels": [panel for panel in found if not isinstance(panel, ChartPanel)],
        "has_tiles": bool(tiles.held(ctx)),
        "period": shown,
        "periods": period.CHOICES,
        "name": _greeting_name(request),
        "draws_charts": any(panel.kind == "chart" for panel in charts),
    }
    return render(request, "dashboards/home.html", context)


@require_safe
@login_required
def key_figures(request: HttpRequest) -> HttpResponse:
    """The row of key figures; empty for an account none of them reaches."""
    ctx = access_for(request)
    context = {"tiles": tiles.tiles(ctx, period.period_of(request))}
    return render(request, "dashboards/tiles.html", context)


@require_safe
@login_required
def panel(request: HttpRequest, key: str) -> HttpResponse:
    found = panels.BY_KEY.get(key)
    if found is None:
        raise Http404
    for capability in panels.capabilities(found):
        require(request, capability)
    ctx = access_for(request)
    if isinstance(found, ChartPanel):
        shown = period.period_of(request)
        context = {"panel": found, "chart": found.build(ctx, shown), "period": shown}
        return render(request, "dashboards/chart.html", context)
    context = {"panel": found, "content": found.build(ctx)}
    return render(request, "dashboards/panel.html", context)

"""The home page, the administration overview, the key figures and the lazily loaded panels
(plans M7.2, M8b.5, M14.3).

All read-only (GET and HEAD). The home page lists the day-to-day panels the user's
capabilities reach; the administration overview (``org.manage``) lists the organisation's own,
each panel on exactly one of the two boards (``apps.dashboards.boards``);
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
from apps.access.mixins import require, require_capability
from apps.access.roles import Capability
from apps.dashboards import panels, period, tiles
from apps.dashboards.boards import Board
from apps.dashboards.chart import ChartPanel


def _greeting_name(request: HttpRequest) -> str:
    user = request.user
    name = getattr(user, "full_name", "") or ""
    return name.split()[0] if name.strip() else getattr(user, "email", "")


def _board_context(request: HttpRequest, board: Board) -> dict[str, object]:
    """What both boards draw: the charts and the task panels of ``board``, the period."""
    found = panels.visible(access_for(request), board)
    charts = [panel for panel in found if isinstance(panel, ChartPanel)]
    return {
        "charts": charts,
        "panels": [panel for panel in found if not isinstance(panel, ChartPanel)],
        "period": period.period_of(request),
        "periods": period.CHOICES,
        "draws_charts": any(panel.kind == "chart" for panel in charts),
    }


@require_safe
@login_required
def home(request: HttpRequest) -> HttpResponse:
    """Landing page after login: the key figures, the charts and the task panels, each a
    placeholder that loads its own fragment."""
    context = _board_context(request, Board.HOME)
    context["has_tiles"] = bool(tiles.held(access_for(request)))
    context["name"] = _greeting_name(request)
    return render(request, "dashboards/home.html", context)


@require_safe
@require_capability(Capability.ORG_MANAGE)
def administration(request: HttpRequest) -> HttpResponse:
    """The organisation's overview: the panels that moved off the home page (Q81). No key
    figures: those stay on home."""
    return render(
        request, "dashboards/administration.html", _board_context(request, Board.ADMINISTRATION)
    )


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

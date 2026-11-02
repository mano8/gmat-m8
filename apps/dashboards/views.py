"""The home page and its lazily loaded panels (plan M7.2).

Both are read-only. The home page lists the panels the user's capabilities reach; a panel's
fragment needs that panel's capability (403 otherwise, 404 for an unknown key), and its rows
come from selectors that apply the capability's scope.
"""

from django.contrib.auth.decorators import login_required
from django.http import Http404, HttpRequest, HttpResponse
from django.shortcuts import render
from django.views.decorators.http import require_safe

from apps.access.middleware import access_for
from apps.access.mixins import require
from apps.dashboards import panels


@require_safe
@login_required
def home(request: HttpRequest) -> HttpResponse:
    """Landing page after login: one placeholder per panel, each loading its own fragment."""
    context = {"panels": panels.visible(access_for(request))}
    return render(request, "dashboards/home.html", context)


@require_safe
@login_required
def panel(request: HttpRequest, key: str) -> HttpResponse:
    found = panels.BY_KEY.get(key)
    if found is None:
        raise Http404
    require(request, found.capability)
    context = {"panel": found, "content": found.build(access_for(request))}
    return render(request, "dashboards/panel.html", context)

"""The project's URLs plus views that commit the violations of M2.4's "done when".

Used only by ``test_matrix_guards.py`` to show that the matrix fails on each of them.
"""

from uuid import UUID

from django.http import HttpRequest, HttpResponse
from django.shortcuts import get_object_or_404
from django.urls import path

from apps.access.middleware import access_for
from apps.access.mixins import mutating, require_capability
from apps.access.principals import SessionPrincipal, SystemPrincipal
from apps.access.roles import Capability
from apps.projects import services
from apps.projects.models import Project
from config.urls import urlpatterns as project_urlpatterns
from tests.org import Sentinel

VIEW, MANAGE = Capability.PROJECT_VIEW, Capability.PROJECT_MANAGE


@require_capability(VIEW)
def project_scoped(request: HttpRequest, pk: UUID) -> HttpResponse:
    project = get_object_or_404(Project.objects.visible_to(access_for(request), VIEW), pk=pk)
    return HttpResponse(project.code)


@require_capability(VIEW)
def project_unscoped(request: HttpRequest, pk: UUID) -> HttpResponse:
    """Violation: the row is read without ``visible_to``."""
    return HttpResponse(get_object_or_404(Project, pk=pk).code)


@require_capability(VIEW)
def price_leak(request: HttpRequest) -> HttpResponse:
    """Violation: a price, as the Spanish locale renders it, on a page without ``price.view``."""
    whole, decimals = Sentinel.PRICE.value.split(".")
    return HttpResponse(f"<td>{whole[:2]}.{whole[2:]},{decimals[:2]} €</td>")


@mutating(MANAGE)
def hold_project(request: HttpRequest, pk: UUID) -> HttpResponse:
    services.update_project(SessionPrincipal.from_request(request), pk, status="ON_HOLD")
    return HttpResponse("held")


@require_capability(MANAGE)
def hold_project_on_get(request: HttpRequest, pk: UUID) -> HttpResponse:
    """Violation: a service call from a view that answers GET."""
    services.update_project(SessionPrincipal.from_request(request), pk, status="ON_HOLD")
    return HttpResponse("held")


@mutating(MANAGE)
def worker_in_view(request: HttpRequest) -> HttpResponse:
    """Violation: a system principal constructed in a view."""
    SystemPrincipal()
    return HttpResponse("sent")


urlpatterns = [
    *project_urlpatterns,
    path("demo/projects/<uuid:pk>/", project_scoped, name="demo_project"),
    path("demo/projects/<uuid:pk>/unscoped/", project_unscoped, name="demo_project_unscoped"),
    path("demo/price/", price_leak, name="demo_price"),
    path("demo/projects/<uuid:pk>/hold/", hold_project, name="demo_hold"),
    path("demo/projects/<uuid:pk>/hold-on-get/", hold_project_on_get, name="demo_hold_on_get"),
    path("demo/worker/", worker_in_view, name="demo_worker"),
]

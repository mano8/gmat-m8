"""A URLconf whose view fails, for the logging sentinel test (500 path)."""

from django.http import HttpRequest, HttpResponse
from django.urls import include, path


def explode(request: HttpRequest, token: str) -> HttpResponse:
    raise RuntimeError(f"failed while handling {request.get_full_path()}")


urlpatterns = [
    path("boom/<str:token>/", explode),
    path("", include("config.urls")),
]

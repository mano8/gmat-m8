"""A signed-in person's own sessions (M8.3, X5). Under ``/account/sessions/``, so that a
privileged account needs session MFA here as on every page (``account_`` URL names are exempt
from it, and these are not such names)."""

from django.urls import URLPattern, path

from apps.accounts import views

app_name = "sessions"

urlpatterns: list[URLPattern] = [
    path("", views.sessions_list, name="list"),
    path("end-others/", views.sessions_end_others, name="end_others"),
    path("<int:session_id>/end/", views.session_end, name="end"),
]

"""The admin site: allauth login, session MFA, and no Django login form (plan §5)."""

from typing import Any

from allauth.account.decorators import secure_admin_login
from django.contrib import admin
from django.http import HttpRequest, HttpResponse, HttpResponseRedirect
from django.shortcuts import resolve_url
from django.urls import reverse

from apps.accounts.mfa import session_passed_mfa


class SecureAdminSite(admin.AdminSite):
    """The default ``AdminSite`` (installed through ``apps.accounts.apps.AdminConfig``).

    - **Login** goes through allauth (``secure_admin_login``): anonymous visitors are sent
      to the allauth login page, so rate limits and MFA apply. Django's admin login form,
      which would authenticate with a password only, is never rendered or processed.
    - **Permission** needs an active staff account whose *current session* completed MFA;
      ``MFARequiredMiddleware`` sends any other session to the MFA step-up first. M2.3
      admits ``GROUP_ADMIN``.
    - **Logout** hands over to allauth's logout, which sends ``Clear-Site-Data``.
    """

    site_header = "gmat-m8"
    site_title = "gmat-m8"
    index_title = "Administration"

    def has_permission(self, request: HttpRequest) -> bool:
        user = request.user
        return bool(user.is_active and user.is_staff and session_passed_mfa(request))

    def login(
        self, request: HttpRequest, extra_context: dict[str, Any] | None = None
    ) -> HttpResponse:
        index = reverse("admin:index", current_app=self.name)

        def authenticated_staff(request: HttpRequest) -> HttpResponse:
            next_url = request.GET.get("next", "")
            return HttpResponseRedirect(next_url if next_url.startswith(index) else index)

        guarded = secure_admin_login(authenticated_staff)  # type: ignore[no-untyped-call]
        response: HttpResponse = guarded(request)
        return response

    def logout(  # type: ignore[override]  # a redirect, not Django's TemplateResponse
        self, request: HttpRequest, extra_context: dict[str, Any] | None = None
    ) -> HttpResponse:
        response = HttpResponseRedirect(resolve_url("account_logout"))
        if request.method == "POST":
            # 307 replays the POST, with its CSRF token, against allauth's logout.
            response.status_code = 307
        return response

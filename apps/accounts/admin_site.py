"""The admin site: allauth login, session MFA, and no Django login form (plan §5)."""

from typing import Any

from allauth.account.decorators import secure_admin_login
from django.contrib import admin
from django.core.exceptions import PermissionDenied
from django.http import HttpRequest, HttpResponse, HttpResponseRedirect
from django.shortcuts import resolve_url
from django.urls import reverse

from apps.access.authorization import may_use_admin
from apps.access.middleware import access_for


class SecureAdminSite(admin.AdminSite):
    """The default ``AdminSite`` (installed through ``apps.accounts.apps.AdminConfig``).

    - **Login** goes through allauth: anonymous visitors are sent to the allauth login
      page (``secure_admin_login``), so rate limits and MFA apply. Django's admin login form,
      which would authenticate with a password only, is never rendered or processed.
    - **Permission** needs an active superuser or a holder of ``org.manage`` (a group
      administrator), as the access engine resolves it, whose *current session* completed
      MFA after their last privilege elevation; ``MFARequiredMiddleware`` sends any other
      session to the MFA step-up first. Both are read once per request (``request.access``).
    - **Logout** hands over to allauth's logout, which sends ``Clear-Site-Data``.
    """

    site_header = "gmat-m8"
    site_title = "gmat-m8"
    index_title = "Administration"

    def has_permission(self, request: HttpRequest) -> bool:
        ctx = access_for(request)
        return may_use_admin(ctx) and ctx.mfa_passed

    def login(
        self, request: HttpRequest, extra_context: dict[str, Any] | None = None
    ) -> HttpResponse:
        """Anonymous visitors go to allauth's login; admitted users go to ``next``.

        allauth's ``secure_admin_login`` refuses every authenticated user who is not
        ``is_staff``, which a group administrator is not, so it only handles the anonymous
        case here; ``may_use_admin`` decides for an authenticated user (403 otherwise).
        """
        index = reverse("admin:index", current_app=self.name)
        if request.user.is_authenticated:
            if not may_use_admin(access_for(request)):
                raise PermissionDenied
            next_url = request.GET.get("next", "")
            return HttpResponseRedirect(next_url if next_url.startswith(index) else index)
        guarded = secure_admin_login(lambda request: HttpResponse())  # type: ignore[no-untyped-call]
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

"""The signed-in user's notification settings (M7.1).

The page reads through ``selectors``; the save is its own POST-only URL that calls the
service, which authorizes again. Every signed-in member may change their own choice.
"""

from django.http import HttpRequest, HttpResponse
from django.shortcuts import render
from django.utils.translation import gettext as _
from django.views.decorators.http import require_safe

from apps.access.middleware import access_for
from apps.access.mixins import mutating, require_capability
from apps.access.roles import Capability
from apps.core.responses import perform
from apps.notifications import selectors, services

SELF = Capability.CATALOG_VIEW


@require_safe
@require_capability(SELF)
def settings_page(request: HttpRequest) -> HttpResponse:
    context = {"email_opt_out": selectors.email_opt_out(access_for(request))}
    return render(request, "notifications/settings.html", context)


@mutating(SELF)
def settings_save(request: HttpRequest) -> HttpResponse:
    opt_out = request.POST.get("business_email") != "on"
    principal = access_for(request).principal
    return perform(
        request,
        lambda: services.set_email_opt_out(principal, opt_out),
        _("Your notification settings were saved."),
        "notifications:settings",
    )

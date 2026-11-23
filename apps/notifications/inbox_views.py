"""The signed-in person's in-app notifications (M17.4, Q94, Q99): the bell's count, the list, and
the two POSTs that read them.

Every read goes through ``selectors`` (``in_app.visible``: the person's own rows whose request
they may still see) and every change through ``services`` (``reading``), which take no user id:
another person's notification is a 404, never a 403. The bell's fragment is GET-only and
``session_passive``: a page that polls by itself must not keep the session alive.
"""

from uuid import UUID

from django.contrib import messages
from django.http import HttpRequest, HttpResponse
from django.shortcuts import redirect, render
from django.utils.translation import gettext as _
from django.utils.translation import ngettext
from django.views.decorators.http import require_safe

from apps.access.middleware import access_for
from apps.access.mixins import mutating, require_capability
from apps.access.roles import Capability
from apps.accounts.sessions import session_passive
from apps.core.listing import ListScope, paginate, render_list
from apps.notifications import selectors, services

SELF = Capability.CATALOG_VIEW
PER_PAGE = 25
SCOPE = ListScope(sized=False)  # a fixed size: the list has no toolbar


@session_passive
@require_safe
@require_capability(SELF)
def bell(request: HttpRequest) -> HttpResponse:
    """The bell with its count, reloaded by the page every minute."""
    unread = selectors.unread_count(access_for(request))
    return render(request, "notifications/bell.html", {"unread": unread})


@require_safe
@require_capability(SELF)
def inbox(request: HttpRequest) -> HttpResponse:
    ctx = access_for(request)
    listing = paginate(
        request, selectors.in_app_notifications(ctx), default_per_page=PER_PAGE, scope=SCOPE
    )
    context = {"listing": listing, "unread": selectors.unread_count(ctx)}
    return render_list(request, "notifications/inbox.html", context)


@mutating(SELF)
def open_notification(request: HttpRequest, notification_id: UUID) -> HttpResponse:
    """Mark one read and go to its request."""
    page = services.open_notification(access_for(request).principal, notification_id)
    return redirect(page)


@mutating(SELF)
def mark_all_read(request: HttpRequest) -> HttpResponse:
    count = services.mark_all_read(access_for(request).principal)
    messages.success(
        request,
        ngettext(
            "%(count)d notification marked as read.",
            "%(count)d notifications marked as read.",
            count,
        )
        % {"count": count}
        if count
        else _("Nothing to mark as read."),
    )
    return redirect("inapp:list")

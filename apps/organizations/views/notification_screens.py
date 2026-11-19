"""Which business emails are sent (M15.3, Q87-Q89): the administration page, the setting for
everyone and a person's settings.

``org.manage`` with session MFA for every page and action (403 otherwise; an unknown email is
404, as is a person out of reach). Reads go through the selectors of ``apps.notifications``;
writes through its services from POST-only views, each on its own URL, answering by the dialog
contract of ``apps.core.responses``. A staff account's settings are changed by nobody here (the
service refuses). Nothing is sent to the person.
"""

from typing import Any
from uuid import UUID

from django.http import Http404, HttpRequest, HttpResponse
from django.shortcuts import render
from django.urls import reverse
from django.utils.translation import gettext as _
from django.views.decorators.http import require_safe

from apps.access.context import AccessContext
from apps.access.middleware import access_for
from apps.access.mixins import mutating, require_capability
from apps.access.policy import is_staff_account
from apps.access.roles import Capability
from apps.accounts import selectors as account_selectors
from apps.accounts.models import User
from apps.core.responses import invalid, perform
from apps.notifications import selectors, services
from apps.notifications.kinds import Notice
from apps.organizations.notice_dialogs import (
    notice_default_dialog,
    notice_slug,
    person_notices_dialog,
)
from apps.organizations.notice_forms import NoticeDefaultForm, PersonNoticesForm
from apps.organizations.row_dialogs import person_url

ORG = Capability.ORG_MANAGE
DEFAULT_IDS = "id_notice_%s_%%s"
PERSON_IDS = "id_notices_%s"


def _default_ids(notice: Notice) -> str:
    return DEFAULT_IDS % notice_slug(notice).replace("-", "_")


def _on_off(enabled: bool) -> str:
    return "on" if enabled else "off"


def person_section(ctx: AccessContext, user: User, *, editable: bool) -> dict[str, Any]:
    """What the person's page shows of their email notifications: every notice with its state and
    why, and the dialog that changes them. Nothing for a staff account (nobody changes its
    settings) or an account that has left; no dialog where the page may not change the person."""
    if is_staff_account(user) or not user.is_active:
        return {"notices": None, "notices_dialog": None}
    items = selectors.person_notices(ctx, user)
    modes = {item.spec.notice: item.mode for item in items}
    form = PersonNoticesForm(
        initial=PersonNoticesForm.initial_for(modes), actor_id=ctx.user_id, auto_id=PERSON_IDS
    )
    return {
        "notices": items,
        "notices_dialog": person_notices_dialog(user, form) if editable else None,
    }


def _known(value: str) -> Notice:
    try:
        return Notice(value)
    except ValueError:
        raise Http404("Unknown email") from None


def _is_off_now(ctx: AccessContext, notice: Notice) -> bool:
    """Whether ``notice`` is off for everyone now: what an invalid form's dialog must offer."""
    return any(
        not state.enabled for state in selectors.notice_states(ctx) if state.spec.notice == notice
    )


@require_safe
@require_capability(ORG)
def notifications(request: HttpRequest) -> HttpResponse:
    """Every email, who receives it, its state for everyone, and one dialog to flip it."""
    ctx = access_for(request)
    rows = []
    for state in selectors.notice_states(ctx):
        notice = state.spec.notice
        form = NoticeDefaultForm(
            initial={"enabled": _on_off(not state.enabled)},
            actor_id=ctx.user_id,
            auto_id=_default_ids(notice),
        )
        rows.append((state, notice_default_dialog(notice, form, turn_on=not state.enabled)))
    return render(request, "organizations/notifications.html", {"rows": rows})


@mutating(ORG)
def notification_default(request: HttpRequest, notice: str) -> HttpResponse:
    ctx = access_for(request)
    kind = _known(notice)
    form = NoticeDefaultForm(request.POST, auto_id=_default_ids(kind))
    turn_on = form.is_on() if form.is_valid() else _is_off_now(ctx, kind)
    dialog = notice_default_dialog(kind, form, turn_on=turn_on).dialog()
    to = reverse("org:notifications")
    if not form.is_valid():
        return invalid(request, form, to, dialog=dialog)
    return perform(
        request,
        lambda: services.set_notice_default(
            ctx.principal,
            kind,
            enabled=turn_on,
            idempotency_key=form.cleaned_data["idempotency_key"],
        ),
        _("Sent to everyone again.") if turn_on else _("No longer sent to everyone."),
        to,
        dialog=dialog,
    )


@mutating(ORG)
def person_notifications(request: HttpRequest, user_id: UUID) -> HttpResponse:
    ctx = access_for(request)
    user = account_selectors.person(ctx, user_id)
    form = PersonNoticesForm(request.POST, auto_id=PERSON_IDS)
    dialog = person_notices_dialog(user, form).dialog()
    to = person_url(user.pk)
    if not form.is_valid():
        return invalid(request, form, to, dialog=dialog)
    return perform(
        request,
        lambda: services.set_person_notices(
            ctx.principal,
            user.pk,
            form.modes(),
            idempotency_key=form.cleaned_data["idempotency_key"],
        ),
        _("Email notifications saved."),
        to,
        dialog=dialog,
    )

"""Which business notifications are sent (M15.3, M17.4: Q87-Q89, Q93, Q97): the administration
page, the setting for everyone and a person's settings, per channel.

``org.manage`` with session MFA for every page and action (403 otherwise; an unknown notification
or channel is 404, as is a person out of reach). Reads go through the selectors of
``apps.notifications``; writes through its services from POST-only views, each on its own URL,
answering by the dialog contract of ``apps.core.responses``. A staff account's settings are
changed by nobody here (the service refuses). Nothing is sent to the person.
"""

from typing import Any
from uuid import UUID

from django.contrib import messages
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
from apps.core.responses import go, invalid, perform
from apps.notifications import selectors, services
from apps.notifications.kinds import CHANNELS, Channel, Notice, channel_from_slug, channel_slug
from apps.organizations.notice_dialogs import (
    notice_default_dialog,
    notice_slug,
    person_notices_dialog,
)
from apps.organizations.notice_forms import NoticeDefaultForm, PersonNoticesForm
from apps.organizations.row_dialogs import person_url

ORG = Capability.ORG_MANAGE
DEFAULT_IDS = "id_notice_%s_%%s"
PERSON_IDS = "id_notices_%s_%%s"


def _default_ids(notice: Notice) -> str:
    return DEFAULT_IDS % notice_slug(notice).replace("-", "_")


def _person_ids(channel: Channel) -> str:
    return PERSON_IDS % channel_slug(channel).replace("-", "_")


def _default_form(ctx: AccessContext, state: selectors.NoticeState) -> NoticeDefaultForm:
    return NoticeDefaultForm(
        initial=NoticeDefaultForm.initial_for({cell.channel: cell.enabled for cell in state.cells}),
        actor_id=ctx.user_id,
        auto_id=_default_ids(state.spec.notice),
    )


def person_section(ctx: AccessContext, user: User, *, editable: bool) -> dict[str, Any]:
    """What the person's page shows of their notifications: every notification on every channel
    with its state and why, and a dialog per channel that changes the administrators' settings
    for them. Nothing for a staff account (nobody changes its settings) or an account that has
    left; no dialog where the page may not change the person."""
    if is_staff_account(user) or not user.is_active:
        return {"notices": None, "notices_dialogs": []}
    rows = selectors.person_notices(ctx, user)
    return {
        "notices": rows,
        "notices_dialogs": [
            person_notices_dialog(user, channel, _person_form(ctx, rows, channel))
            for channel in CHANNELS
        ]
        if editable
        else [],
    }


def _person_form(
    ctx: AccessContext, rows: list[selectors.NoticeRow], channel: Channel, data: Any = None
) -> PersonNoticesForm:
    return PersonNoticesForm(
        data,
        channel=channel,
        initial=PersonNoticesForm.initial_for(
            {row.spec.notice: row.cell(channel).mode for row in rows}
        ),
        actor_id=ctx.user_id,
        auto_id=_person_ids(channel),
    )


def _known(value: str) -> Notice:
    try:
        return Notice(value)
    except ValueError:
        raise Http404("Unknown notification") from None


def _known_channel(slug: str) -> Channel:
    channel = channel_from_slug(slug)
    if channel is None:
        raise Http404("Unknown channel")
    return channel


@require_safe
@require_capability(ORG)
def notifications(request: HttpRequest) -> HttpResponse:
    """Every notification, who receives it, its state for everyone on each channel, and one
    dialog to change it."""
    ctx = access_for(request)
    rows = [
        (state, notice_default_dialog(state.spec.notice, _default_form(ctx, state)))
        for state in selectors.notice_states(ctx)
    ]
    channels = list(CHANNELS.values())
    return render(request, "organizations/notifications.html", {"rows": rows, "channels": channels})


@mutating(ORG)
def notification_default(request: HttpRequest, notice: str) -> HttpResponse:
    ctx = access_for(request)
    kind = _known(notice)
    state = next(state for state in selectors.notice_states(ctx) if state.spec.notice == kind)
    form = NoticeDefaultForm(request.POST, auto_id=_default_ids(kind))
    dialog = notice_default_dialog(kind, form).dialog()
    to = reverse("org:notifications")
    if not form.is_valid():
        return invalid(request, form, to, dialog=dialog)
    changes = form.changes()
    if not changes:
        messages.info(request, _("Nothing changed."))
        return go(request, to)
    return perform(
        request,
        lambda: services.set_notice_channels(
            ctx.principal,
            state.spec.notice,
            changes,
            idempotency_key=form.cleaned_data["idempotency_key"],
        ),
        _("Saved."),
        to,
        dialog=dialog,
    )


@mutating(ORG)
def person_notifications(request: HttpRequest, user_id: UUID, channel: str) -> HttpResponse:
    ctx = access_for(request)
    kind = _known_channel(channel)
    user = account_selectors.person(ctx, user_id)
    form = _person_form(ctx, selectors.person_notices(ctx, user), kind, request.POST)
    dialog = person_notices_dialog(user, kind, form).dialog()
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
        _("Notifications saved."),
        to,
        dialog=dialog,
    )

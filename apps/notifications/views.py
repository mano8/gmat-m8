"""*My notifications* (M7.1, M15.3, M17.4): where the signed-in person is told and what they get.

The page reads through ``selectors``; each part is changed in a dialog that posts to its own
POST-only URL, which calls the service (it authorizes again). Every signed-in member may change
their own choices; only their own, never another person's (the services take no user id). What
the administrators set for them is shown beside, and their own choice wins over it (Q93).
"""

from typing import Any

from django.http import Http404, HttpRequest, HttpResponse
from django.shortcuts import render
from django.urls import reverse
from django.utils.translation import gettext as _
from django.views.decorators.http import require_safe

from apps.access.context import AccessContext
from apps.access.middleware import access_for
from apps.access.mixins import mutating, require_capability
from apps.access.roles import Capability
from apps.core.responses import invalid, perform
from apps.notifications import selectors, services
from apps.notifications.dialogs import channels_dialog, choices_dialog
from apps.notifications.forms import OwnChannelsForm, OwnChoicesForm
from apps.notifications.kinds import CHANNELS, Channel, channel_from_slug, channel_slug

SELF = Capability.CATALOG_VIEW
CHANNEL_IDS = "id_channels_%s"
CHOICE_IDS = "id_choices_%s_%%s"


def _choice_ids(channel: Channel) -> str:
    return CHOICE_IDS % channel_slug(channel).replace("-", "_")


def _choices_form(
    ctx: AccessContext,
    channel: Channel,
    data: Any = None,
    *,
    rows: list[selectors.NoticeRow] | None = None,
) -> OwnChoicesForm:
    """The form of one channel's choices, unbound (from what is stored) or bound to ``data``."""
    rows = selectors.own_notices(ctx) if rows is None else rows
    cells = {row.spec.notice: row.cell(channel) for row in rows}
    form = OwnChoicesForm(
        data,
        channel=channel,
        administrators={notice: cell.administrators for notice, cell in cells.items()},
        initial=OwnChoicesForm.initial_for({notice: cell.choice for notice, cell in cells.items()}),
        actor_id=ctx.user_id,
        auto_id=_choice_ids(channel),
    )
    return form


def _known(slug: str) -> Channel:
    channel = channel_from_slug(slug)
    if channel is None:
        raise Http404("Unknown channel")
    return channel


@require_safe
@require_capability(SELF)
def settings_page(request: HttpRequest) -> HttpResponse:
    ctx = access_for(request)
    channels = selectors.own_channels(ctx)
    where = OwnChannelsForm(
        initial=OwnChannelsForm.initial_for(
            {state.spec.channel: state.enabled for state in channels}
        ),
        actor_id=ctx.user_id,
        auto_id=CHANNEL_IDS,
    )
    rows = selectors.own_notices(ctx)
    context = {
        "channels": channels,
        "channels_dialog": channels_dialog(where),
        "choice_dialogs": [
            choices_dialog(channel, _choices_form(ctx, channel, rows=rows)) for channel in CHANNELS
        ],
        "notices": rows,
    }
    return render(request, "notifications/settings.html", context)


@mutating(SELF)
def save_channels(request: HttpRequest) -> HttpResponse:
    principal = access_for(request).principal
    form = OwnChannelsForm(request.POST, auto_id=CHANNEL_IDS)
    dialog = channels_dialog(form).dialog()
    to = reverse("notifications:settings")
    if not form.is_valid():
        return invalid(request, form, to, dialog=dialog)
    return perform(
        request,
        lambda: services.set_own_channels(
            principal, form.changes(), idempotency_key=form.cleaned_data["idempotency_key"]
        ),
        _("Your notification settings were saved."),
        to,
        dialog=dialog,
    )


@mutating(SELF)
def save_choices(request: HttpRequest, channel: str) -> HttpResponse:
    ctx = access_for(request)
    kind = _known(channel)
    form = _choices_form(ctx, kind, request.POST)
    dialog = choices_dialog(kind, form).dialog()
    to = reverse("notifications:settings")
    if not form.is_valid():
        return invalid(request, form, to, dialog=dialog)
    return perform(
        request,
        lambda: services.set_own_notices(
            ctx.principal,
            form.choices_made(),
            idempotency_key=form.cleaned_data["idempotency_key"],
        ),
        _("Your notification settings were saved."),
        to,
        dialog=dialog,
    )

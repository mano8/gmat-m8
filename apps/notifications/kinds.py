"""The business notices: the registry of what the application tells people (M15, Q88, Q90).

Every business notification belongs to one ``Notice``, a stable key stored on the queued
message (``EmailOutbox.notice``) and on the settings that decide who receives it
(``NoticeDefault`` for everyone, ``NoticeSetting`` per person; ``apps.notifications.resolution``).
Security mail (invitations, email changes, password and second-factor notices, resets) and
supplier dispatches are not notices: they are never optional.

``NOTICES`` says, per notice and in the order the screens list them, its name, who receives
it, when, the page of the request it leads to and the capability it is received through (the
message's eligibility rule, checked again by the worker at the claim). A later business notice
joins the registry with a key of its own; its default is on until an administrator turns it off.

The settings are stored per ``Channel`` (M17, Q95): email and in the application. ``CHANNELS``
is the one place that says which channels the installation offers and what each gives a person
who never chose (Q96); the resolution, the services and the screens loop over it, so the planned
Telegram bot adds its channel here, with ``opt_in`` (off until the person turns it on), without
reshaping a table or a screen.
"""

from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType

from django.db import models
from django.utils.functional import Promise
from django.utils.translation import gettext_lazy as _

from apps.access.roles import Capability

SUBJECT = "material_requests.materialrequest"
"""What every business notice is about (``app_label.model``): a material request. The in-app
notifications store it with the request's id (``InAppNotification``); a notice about another kind
of object would add its type here and to the in-app selectors."""


class Notice(models.TextChoices):
    REQUEST_TO_REVIEW = "REQUEST_TO_REVIEW", _("A request to review")
    REQUEST_ASSIGNED = "REQUEST_ASSIGNED", _("A request assigned to me")
    REQUEST_APPROVED = "REQUEST_APPROVED", _("My request approved")
    REQUEST_REJECTED = "REQUEST_REJECTED", _("My request rejected")
    REQUEST_EDITED = "REQUEST_EDITED", _("My request changed by a reviewer")
    REQUEST_HANDED_OVER = "REQUEST_HANDED_OVER", _("Goods handed over or received on site")


class Channel(models.TextChoices):
    EMAIL = "EMAIL", _("Email")
    IN_APP = "IN_APP", _("In the application")


class Mode(models.TextChoices):
    """A person's setting for one notice; no setting means: follow the one for everyone."""

    ALWAYS = "ALWAYS", _("Always")
    NEVER = "NEVER", _("Never")


@dataclass(frozen=True)
class NoticeSpec:
    """What the screens say about a notice, and what it is received through."""

    notice: Notice
    recipients: Promise
    when: Promise
    capability: Capability
    order: int
    page: str  # the URL name of the object's page the notice leads to (email link, in-app open)

    @property
    def label(self) -> str:
        return str(self.notice.label)


def _registry(*specs: NoticeSpec) -> Mapping[Notice, NoticeSpec]:
    ordered = sorted(specs, key=lambda spec: spec.order)
    if [spec.notice for spec in ordered] != list(Notice):
        raise ValueError("Every notice needs exactly one entry, in the order of Notice")
    return MappingProxyType({spec.notice: spec for spec in ordered})


NOTICES: Mapping[Notice, NoticeSpec] = _registry(
    NoticeSpec(
        Notice.REQUEST_TO_REVIEW,
        _("Everyone who can review the request, except its author and its assigned reviewer"),
        _("Each request submitted"),
        Capability.REQUEST_REVIEW,
        10,
        page="requests:examine",
    ),
    NoticeSpec(
        Notice.REQUEST_ASSIGNED,
        _("The reviewer the request is assigned to"),
        _("At submit when a reviewer is chosen, and at each reassignment"),
        Capability.REQUEST_REVIEW,
        20,
        page="requests:examine",
    ),
    NoticeSpec(
        Notice.REQUEST_APPROVED,
        _("The requester"),
        _("Each approval"),
        Capability.REQUEST_VIEW,
        30,
        page="requests:detail",
    ),
    NoticeSpec(
        Notice.REQUEST_REJECTED,
        _("The requester"),
        _("Each rejection"),
        Capability.REQUEST_VIEW,
        40,
        page="requests:detail",
    ),
    NoticeSpec(
        Notice.REQUEST_EDITED,
        _("The requester"),
        _("A reviewer's edits, grouped over 10 minutes"),
        Capability.REQUEST_VIEW,
        50,
        page="requests:detail",
    ),
    NoticeSpec(
        Notice.REQUEST_HANDED_OVER,
        _("The requester of each request served"),
        _("Each handover, and each receipt on site"),
        Capability.REQUEST_VIEW,
        60,
        page="requests:detail",
    ),
)


@dataclass(frozen=True)
class ChannelSpec:
    """A channel the installation offers (M17, Q95, Q96).

    ``default``: whether a notice is sent on the channel when no level decided (no setting for
    everyone, for the person or by the person). ``opt_in``: the person's switch for the channel
    is off until they turn it on (none yet; the Telegram bot's will be); otherwise it is on
    until they turn it off.
    """

    channel: Channel
    default: bool
    opt_in: bool
    order: int

    @property
    def label(self) -> str:
        return str(self.channel.label)


def _channels(*specs: ChannelSpec) -> Mapping[Channel, ChannelSpec]:
    ordered = sorted(specs, key=lambda spec: spec.order)
    if [spec.channel for spec in ordered] != list(Channel):
        raise ValueError("Every channel needs exactly one entry, in the order of Channel")
    return MappingProxyType({spec.channel: spec for spec in ordered})


CHANNELS: Mapping[Channel, ChannelSpec] = _channels(
    ChannelSpec(Channel.EMAIL, default=True, opt_in=False, order=10),
    ChannelSpec(Channel.IN_APP, default=True, opt_in=False, order=20),
)


def channel_slug(channel: Channel) -> str:
    """The channel as a URL segment and an element id (``IN_APP`` becomes ``in-app``)."""
    return channel.value.lower().replace("_", "-")


def channel_from_slug(slug: str) -> Channel | None:
    """The channel a URL segment names, or ``None`` (an unknown one is a 404 for the view)."""
    return next((channel for channel in CHANNELS if channel_slug(channel) == slug), None)

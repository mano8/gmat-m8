"""The business notices: the registry of what the application tells people (M15, Q88, Q90).

Every business notification belongs to one ``Notice``, a stable key stored on the queued
message (``EmailOutbox.notice``) and on the settings that decide who receives it
(``NoticeDefault`` for everyone, ``NoticeSetting`` per person; ``apps.notifications.resolution``).
Security mail (invitations, email changes, password and second-factor notices, resets) and
supplier dispatches are not notices: they are never optional.

``NOTICES`` says, per notice and in the order the screens list them, its name, who receives
it, when, and the capability it is received through (the message's eligibility rule, checked
again by the worker at the claim). A later business notice joins the registry with a key of
its own; its default is on until an administrator turns it off.

The settings are stored per ``Channel``: email only for now; the planned Telegram bot adds
its channel to the same registry (Q90).
"""

from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType

from django.db import models
from django.utils.functional import Promise
from django.utils.translation import gettext_lazy as _

from apps.access.roles import Capability


class Notice(models.TextChoices):
    REQUEST_TO_REVIEW = "REQUEST_TO_REVIEW", _("A request to review")
    REQUEST_ASSIGNED = "REQUEST_ASSIGNED", _("A request assigned to me")
    REQUEST_APPROVED = "REQUEST_APPROVED", _("My request approved")
    REQUEST_REJECTED = "REQUEST_REJECTED", _("My request rejected")
    REQUEST_EDITED = "REQUEST_EDITED", _("My request changed by a reviewer")
    REQUEST_HANDED_OVER = "REQUEST_HANDED_OVER", _("Goods handed over or received on site")


class Channel(models.TextChoices):
    EMAIL = "EMAIL", _("Email")


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
    ),
    NoticeSpec(
        Notice.REQUEST_ASSIGNED,
        _("The reviewer the request is assigned to"),
        _("At submit when a reviewer is chosen, and at each reassignment"),
        Capability.REQUEST_REVIEW,
        20,
    ),
    NoticeSpec(
        Notice.REQUEST_APPROVED,
        _("The requester"),
        _("Each approval"),
        Capability.REQUEST_VIEW,
        30,
    ),
    NoticeSpec(
        Notice.REQUEST_REJECTED,
        _("The requester"),
        _("Each rejection"),
        Capability.REQUEST_VIEW,
        40,
    ),
    NoticeSpec(
        Notice.REQUEST_EDITED,
        _("The requester"),
        _("A reviewer's edits, grouped over 10 minutes"),
        Capability.REQUEST_VIEW,
        50,
    ),
    NoticeSpec(
        Notice.REQUEST_HANDED_OVER,
        _("The requester of each request served"),
        _("Each handover, and each receipt on site"),
        Capability.REQUEST_VIEW,
        60,
    ),
)

"""A person's own channels and choices (M17.2, Q93, Q97).

``set_own_channels`` is the signed-in person's switch per channel (level 1 of Q93) and
``set_own_notices`` their own choice per notification and channel (level 2); email's switch is
still ``NotificationPreference.email_opt_out`` (M7.1). Both act on the principal's own rows and
take no user id, so nobody else can be named. Any current member may use them (``catalog.view``,
held by every role; session MFA for a privileged role, as everywhere), each is idempotent when
given a key, writes and audits only what differs from what is stored (``notification.own_changed``
with before and after, branchless) and publishes nothing when nothing changed.
"""

from collections.abc import Mapping
from typing import cast

from django.core.exceptions import ValidationError
from django.utils.translation import gettext_lazy as _

from apps.access.authorization import authorize_in_txn
from apps.access.generations import lock_access_state, lock_users
from apps.access.principals import SessionPrincipal
from apps.access.roles import Capability
from apps.accounts.models import User
from apps.audit.recorder import record
from apps.core import events
from apps.core.db import db_now
from apps.core.idempotency import execute_once
from apps.core.locking import LockMode, LockOrder, transactional
from apps.notifications.kinds import CHANNELS, Channel, Notice
from apps.notifications.models import ChannelSwitch, NoticeChoice, NotificationPreference
from apps.notifications.services.common import NoticeKey, by_channel, flat, known, known_key

OWN_CHANNELS_OPERATION = "notice.own_channels_set"
OWN_NOTICES_OPERATION = "notice.own_notices_set"
OWN_CHANGED = "notification.own_changed"


def _lock_own(principal: SessionPrincipal) -> User:
    """The principal's user row at L0 before the check: two tabs of one person queue behind each
    other, so the unique rows they insert never collide. Settings are not authority."""
    locks = LockOrder()
    lock_access_state(locks)
    lock_users(locks, {principal.user_id}, mode=LockMode.NO_KEY_UPDATE)
    return authorize_in_txn(principal, Capability.CATALOG_VIEW, locks=locks)


def _flag(value: object, field: str) -> bool:
    """``value`` when it is a real boolean, otherwise a validation error on ``field``."""
    if isinstance(value, bool):
        return value
    raise ValidationError({field: ValidationError(_("Unknown choice."), code="invalid_choice")})


def _email_state(user: User) -> NotificationPreference | None:
    return NotificationPreference.objects.select_for_update().filter(user=user).first()


def _write_email(user: User, row: NotificationPreference | None, enabled: bool) -> None:
    if row is None:
        NotificationPreference.objects.create(user=user, email_opt_out=not enabled)
    else:
        row.email_opt_out = not enabled
        row.save(update_fields=["email_opt_out", "updated_at"])


def _write_switch(user: User, row: ChannelSwitch | None, channel: Channel, enabled: bool) -> None:
    if row is None:
        ChannelSwitch.objects.create(user=user, channel=channel, enabled=enabled)
    else:
        row.enabled, row.changed_at = enabled, db_now()
        row.save(update_fields=["enabled", "changed_at", "updated_at"])


def _apply_channel(user: User, channel: Channel, enabled: bool) -> dict[str, bool] | None:
    """Make ``channel`` say ``enabled`` for the person; the change as before and after, or
    ``None`` when it already did."""
    if channel == Channel.EMAIL:
        email = _email_state(user)
        before = not (email.email_opt_out if email else False)
        if before != enabled:
            _write_email(user, email, enabled)
    else:
        switch = (
            ChannelSwitch.objects.select_for_update().filter(user=user, channel=channel).first()
        )
        before = switch.enabled if switch else not CHANNELS[channel].opt_in
        if before != enabled:
            _write_switch(user, switch, channel, enabled)
    return None if before == enabled else {"before": before, "after": enabled}


@transactional
def set_own_channels(
    principal: SessionPrincipal,
    channels: Mapping[Channel | str, bool],
    *,
    idempotency_key: str | None = None,
) -> User:
    """Turn the signed-in person's channels on (``True``) or off (``False``): off, nothing of any
    notification reaches them there; their own choices are kept for when it is turned on again.

    ``channels`` names only the channels to change. Security email and supplier dispatches are not
    affected. A channel already as asked writes nothing and is not audited.
    """
    wanted = {
        known(Channel, key, "channels"): _flag(on, "channels") for key, on in channels.items()
    }
    user = _lock_own(principal)

    def execute() -> User:
        changes = {
            channel.value: change
            for channel, on in wanted.items()
            if (change := _apply_channel(user, channel, on)) is not None
        }
        if changes:
            record(principal.audit_identity, OWN_CHANGED, obj=user, data={"channels": changes})
            events.publish(OWN_CHANGED, user_id=user.pk)
        return user

    return execute_once(
        actor_id=principal.user_id,
        key=idempotency_key,
        operation=OWN_CHANNELS_OPERATION,
        payload={"channels": {channel.value: on for channel, on in wanted.items()}},
        execute=execute,
        replay=lambda _type, pk: User.objects.get(pk=pk),
    )


def _clean_choices(
    choices: Mapping[tuple[object, object], bool | None],
) -> dict[NoticeKey, bool | None]:
    return {
        known_key(key, "choices"): None if on is None else _flag(on, "choices")
        for key, on in choices.items()
    }


def _apply_choices(
    user: User, wanted: Mapping[NoticeKey, bool | None]
) -> dict[NoticeKey, dict[str, bool | None]]:
    """Write what differs from the person's rows (locked); return ``{key: {before, after}}``."""
    rows = {
        (Notice(row.notice), Channel(row.channel)): row
        for row in NoticeChoice.objects.select_for_update().filter(user=user)
    }
    changes: dict[NoticeKey, dict[str, bool | None]] = {}
    for key, on in wanted.items():
        row = rows.get(key)
        before = row.enabled if row is not None else None
        if before == on:
            continue
        _write_choice(user, key, row, on)
        changes[key] = {"before": before, "after": on}
    return changes


def _write_choice(user: User, key: NoticeKey, row: NoticeChoice | None, on: bool | None) -> None:
    """Make the person's row for ``key`` say ``on`` (``None``: no row, follow)."""
    notice, channel = key
    if on is None:
        cast(NoticeChoice, row).delete()  # no row and no choice would not differ
    elif row is not None:
        row.enabled, row.changed_at = on, db_now()
        row.save(update_fields=["enabled", "changed_at", "updated_at"])
    else:
        NoticeChoice.objects.create(user=user, notice=notice, channel=channel, enabled=on)


@transactional
def set_own_notices(
    principal: SessionPrincipal,
    choices: Mapping[tuple[object, object], bool | None],
    *,
    idempotency_key: str | None = None,
) -> User:
    """Set the signed-in person's own choice per notification and channel: ``True`` (wanted),
    ``False`` (not wanted) or ``None`` (follow what the administrators set, which deletes the
    person's row, so their setting applies again).

    ``choices`` is keyed ``(notice, channel)`` and names only what changes. A choice wins over the
    administrators' settings in both directions but never over the person's switch for the
    channel. One audit event holds everything that changed; nothing that already is as asked is
    written.
    """
    wanted = _clean_choices(choices)
    user = _lock_own(principal)

    def execute() -> User:
        changes = _apply_choices(user, wanted)
        if changes:
            record(
                principal.audit_identity,
                OWN_CHANGED,
                obj=user,
                data={"notices": by_channel(changes)},
            )
            events.publish(OWN_CHANGED, user_id=user.pk)
        return user

    return execute_once(
        actor_id=principal.user_id,
        key=idempotency_key,
        operation=OWN_NOTICES_OPERATION,
        payload={"choices": flat(wanted)},
        execute=execute,
        replay=lambda _type, pk: User.objects.get(pk=pk),
    )


def set_email_opt_out(principal: SessionPrincipal, opt_out: bool) -> User:
    """Turn the signed-in user's business email off (``True``) or on (``False``): the email case
    of :func:`set_own_channels` (M7.1)."""
    return set_own_channels(principal, {Channel.EMAIL: not opt_out})

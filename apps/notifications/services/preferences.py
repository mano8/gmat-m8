"""What the administrators decide about business notices, per channel (M15.2, M17.2, Q87, Q89).

``set_notice_default`` (the setting for everyone) and ``set_person_notices`` (a person's settings)
are ``org.manage`` operations (session MFA for a privileged role), each idempotent and audited
with before and after; ``apps.notifications.resolution`` reads what they write, when a message
is queued and again at its claim. A person's own channels and choices, which win over both, are
``apps.notifications.services.own``.
"""

from collections.abc import Mapping
from typing import Any, cast
from uuid import UUID

from django.core.exceptions import ValidationError
from django.utils.translation import gettext_lazy as _

from apps.access.authorization import authorize_in_txn
from apps.access.exceptions import NotInScope
from apps.access.generations import lock_access_state, lock_users
from apps.access.policy import require_may_manage_account
from apps.access.principals import Principal
from apps.access.roles import Capability
from apps.accounts.models import User
from apps.audit.recorder import record
from apps.core import events
from apps.core.db import db_now
from apps.core.idempotency import execute_once
from apps.core.locking import LockMode, LockOrder, transactional
from apps.notifications.kinds import CHANNELS, Channel, Mode, Notice
from apps.notifications.models import NoticeDefault, NoticeSetting
from apps.notifications.services.common import NoticeKey, by_channel, flat, known, known_key

# --- what business email is sent: for everyone and for one person (M15.2, Q87, Q89) -----------

NOTICE_DEFAULT_OPERATION = "notice.default_set"
NOTICE_CHANNELS_OPERATION = "notice.channels_set"
PERSON_NOTICES_OPERATION = "notice.person_set"


def _decide(
    principal: Principal, actor: User, kind: Notice, chosen: Channel, enabled: bool
) -> NoticeDefault:
    """Make the setting for everyone of ``kind`` on ``chosen`` say ``enabled`` (row locked);
    audited and published only when it changes what applied."""
    row, created = NoticeDefault.objects.select_for_update().get_or_create(
        notice=kind, channel=chosen, defaults={"enabled": enabled, "changed_by": actor}
    )
    if not created and row.enabled == enabled:
        return row
    before = CHANNELS[chosen].default if created else row.enabled
    if before == enabled:  # the first decision says what no row already meant
        return row
    if not created:
        row.enabled, row.changed_by = enabled, actor
        row.changed_at = db_now()
        row.save(update_fields=["enabled", "changed_by", "changed_at", "updated_at"])
    record(
        principal.audit_identity,
        "notification.default_changed",
        obj=row,
        data={
            "notice": kind.value,
            "channel": chosen.value,
            "before": before,
            "after": enabled,
        },
    )
    events.publish("notification.default_changed", notice=kind.value, enabled=enabled)
    return row


@transactional
def set_notice_default(
    principal: Principal,
    notice: Notice | str,
    *,
    enabled: bool,
    channel: Channel | str = Channel.EMAIL,
    idempotency_key: str | None = None,
) -> NoticeDefault:
    """Send a business notice to everyone (``True``) or to nobody who has no setting of their own
    (``False``) on ``channel`` (``org.manage``, installation-wide as units are, ADR-0030).

    A person's own setting (``set_person_notices``) and their own switch and choices
    (``services.own``) are never changed by it. The row is created by the first decision (no row
    meant the channel's default, so deciding that first only records who decided); a decision
    that changes nothing is not audited and publishes nothing. The person concerned is told
    nothing (Q89).
    """
    kind = known(Notice, notice, "notice")
    chosen = known(Channel, channel, "channel")
    actor = authorize_in_txn(principal, Capability.ORG_MANAGE, locks=LockOrder())
    return execute_once(
        actor_id=principal.user_id,
        key=idempotency_key,
        operation=NOTICE_DEFAULT_OPERATION,
        payload={"notice": kind.value, "channel": chosen.value, "enabled": enabled},
        execute=lambda: _decide(principal, actor, kind, chosen, enabled),
        replay=lambda _type, pk: NoticeDefault.objects.get(pk=pk),
    )


@transactional
def set_notice_channels(
    principal: Principal,
    notice: Notice | str,
    channels: Mapping[Channel, bool] | Mapping[str, bool],
    *,
    idempotency_key: str | None = None,
) -> NoticeDefault:
    """Send a business notice to everyone or to nobody on each of ``channels`` (``True`` or
    ``False``), in one transaction: what :func:`set_notice_default` does for one channel, for the
    several a dialog of the administration page sets at once (M17.4). ``channels`` names only
    what to change, at least one; each is audited as its own change. Returns the row of the first
    channel named."""
    kind = known(Notice, notice, "notice")
    wanted = {known(Channel, key, "channels"): on for key, on in channels.items()}
    if not wanted:
        raise ValidationError({"channels": ValidationError(_("Nothing to change."), code="empty")})
    actor = authorize_in_txn(principal, Capability.ORG_MANAGE, locks=LockOrder())

    def execute() -> NoticeDefault:
        rows = [_decide(principal, actor, kind, channel, on) for channel, on in wanted.items()]
        return rows[0]

    return execute_once(
        actor_id=principal.user_id,
        key=idempotency_key,
        operation=NOTICE_CHANNELS_OPERATION,
        payload={
            "notice": kind.value,
            "channels": {channel.value: on for channel, on in wanted.items()},
        },
        execute=execute,
        replay=lambda _type, pk: NoticeDefault.objects.get(pk=pk),
    )


def _clean_modes(
    modes: Mapping[tuple[Any, Any], Mode | str | None],
) -> dict[NoticeKey, Mode | None]:
    """``modes`` as members: a ``(notice, channel)`` each, ``None`` (follow the setting for
    everyone) or a mode."""
    return {
        known_key(key, "modes"): None if mode in (None, "") else known(Mode, mode, "modes")
        for key, mode in modes.items()
    }


def _lock_person(principal: Principal, user_id: UUID) -> tuple[User, User]:
    """The actor and the person, locked in id order (L0) before ``org.manage`` is checked.

    Settings are not authority, but two administrators changing the same person must queue
    behind each other (the rows they insert are unique per person and notice). A person who does
    not exist is out of scope (404); a staff or superuser account is never changed by a group
    administrator.
    """
    locks = LockOrder()
    lock_access_state(locks)
    lock_users(locks, {principal.user_id, user_id}, mode=LockMode.NO_KEY_UPDATE)
    actor = authorize_in_txn(principal, Capability.ORG_MANAGE, locks=locks)
    person = User.objects.filter(pk=user_id).first()
    if person is None:
        raise NotInScope(Capability.ORG_MANAGE)
    require_may_manage_account(actor, person)
    if not person.is_active or person.offboarded_at is not None:
        raise ValidationError(
            _("This person has left: their email notifications are not changed."), code="left"
        )
    return actor, person


def _write_mode(
    actor: User, person: User, key: NoticeKey, mode: Mode | None, row: NoticeSetting | None
) -> None:
    """Make the person's row for ``key`` say ``mode`` (``None``: no row)."""
    notice, channel = key
    if mode is None:
        cast(NoticeSetting, row).delete()  # no row and no mode would not differ
    elif row is not None:
        row.mode, row.set_by, row.set_at = mode, actor, db_now()
        row.save(update_fields=["mode", "set_by", "set_at", "updated_at"])
    else:
        NoticeSetting.objects.create(
            user=person, notice=notice, channel=channel, mode=mode, set_by=actor
        )


def _apply_modes(
    actor: User, person: User, modes: dict[NoticeKey, Mode | None]
) -> dict[NoticeKey, dict[str, str | None]]:
    """Write what differs from the person's rows (locked); return ``{key: {before, after}}``."""
    rows = {
        (Notice(row.notice), Channel(row.channel)): row
        for row in NoticeSetting.objects.select_for_update().filter(user=person)
    }
    changes: dict[NoticeKey, dict[str, str | None]] = {}
    for key, mode in modes.items():
        row = rows.get(key)
        before = row.mode if row is not None else None
        if before == mode:
            continue
        _write_mode(actor, person, key, mode, row)
        changes[key] = {"before": before, "after": mode.value if mode else None}
    return changes


@transactional
def set_person_notices(
    principal: Principal,
    user_id: UUID,
    modes: Mapping[tuple[Any, Any], Mode | str | None],
    *,
    idempotency_key: str | None = None,
) -> User:
    """Set, for one person, which business notices they always or never receive, per channel
    (``org.manage``): the person's whole table in one transaction and one audit event.

    ``modes`` is keyed ``(notice, channel)`` and names only what to change: ``Mode.ALWAYS``,
    ``Mode.NEVER`` or ``None`` (follow the setting for everyone, which deletes the person's row).
    These are defaults of whoever has not chosen: the person's own switch for the channel and
    their own choices win over them (Q93, ``services.own``). The people an administrator reaches
    are all of them, but a staff or superuser account is not changed here and an unknown person
    is a 404. Nothing is written or audited when nothing differs; the person is told nothing
    (Q89).
    """
    wanted = _clean_modes(modes)
    actor, person = _lock_person(principal, user_id)

    def execute() -> User:
        changes = _apply_modes(actor, person, wanted)
        if changes:
            record(
                principal.audit_identity,
                "notification.person_changed",
                obj=person,
                data={"changes": by_channel(changes)},
            )
            events.publish("notification.person_changed", user_id=person.pk)
        return person

    return execute_once(
        actor_id=principal.user_id,
        key=idempotency_key,
        operation=PERSON_NOTICES_OPERATION,
        payload={
            "user_id": user_id,
            "modes": flat({key: mode.value if mode else None for key, mode in wanted.items()}),
        },
        execute=execute,
        replay=lambda _type, pk: User.objects.get(pk=pk),
    )

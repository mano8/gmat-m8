"""What business email a person gets: their own refusal (M7.1), the setting for everyone and a
person's settings by a group administrator (M15.2, Q87, Q89).

``set_email_opt_out`` is the signed-in user's own switch. ``set_notice_default`` and
``set_person_notices`` are ``org.manage`` operations (session MFA for a privileged role), each
idempotent and audited with before and after; ``apps.notifications.resolution`` reads what they
write, when a message is queued and again at its claim.
"""

from collections.abc import Mapping
from uuid import UUID

from django.core.exceptions import ValidationError
from django.utils.translation import gettext_lazy as _

from apps.access.authorization import authorize_in_txn
from apps.access.exceptions import NotInScope
from apps.access.generations import lock_access_state, lock_users
from apps.access.policy import require_may_manage_account
from apps.access.principals import Principal, SessionPrincipal
from apps.access.roles import Capability
from apps.accounts.models import User
from apps.audit.recorder import record
from apps.core import events
from apps.core.db import db_now
from apps.core.idempotency import execute_once
from apps.core.locking import LockMode, LockOrder, transactional
from apps.notifications.kinds import Channel, Mode, Notice
from apps.notifications.models import NoticeDefault, NoticeSetting, NotificationPreference

# --- the user's own choice about business email (M7.1) -------------------------------------


@transactional
def set_email_opt_out(principal: SessionPrincipal, opt_out: bool) -> NotificationPreference:
    """Turn the signed-in user's business email off (``True``) or on (``False``).

    Any current member may do this for themselves: ``catalog.view`` is the capability every
    role holds, and the row is always the principal's own. Security email and supplier
    dispatches are not affected. A change that alters nothing writes nothing.
    """
    user = authorize_in_txn(principal, Capability.CATALOG_VIEW, locks=LockOrder())
    preference, created = NotificationPreference.objects.select_for_update().get_or_create(
        user=user, defaults={"email_opt_out": opt_out}
    )
    changed = preference.email_opt_out != opt_out if not created else opt_out
    if not changed:
        return preference
    if not created:
        preference.email_opt_out = opt_out
        preference.save(update_fields=["email_opt_out", "updated_at"])
    record(
        principal.audit_identity,
        "notification_preference.changed",
        obj=preference,
        data={"email_opt_out": opt_out},
    )
    return preference


# --- what business email is sent: for everyone and for one person (M15.2, Q87, Q89) -----------

NOTICE_DEFAULT_OPERATION = "notice.default_set"
PERSON_NOTICES_OPERATION = "notice.person_set"


def _known[E: (Notice, Channel, Mode)](kind: type[E], value: str, field: str) -> E:
    """``value`` as a member of ``kind``, or a validation error on ``field``."""
    try:
        return kind(value)
    except ValueError:
        raise ValidationError(
            {field: ValidationError(_("Unknown choice."), code="invalid_choice")}
        ) from None


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

    A person's own setting (``set_person_notices``) and their own refusal of business email
    (``set_email_opt_out``) are never changed by it. The row is created by the first decision (no
    row meant *on*); a decision that changes nothing writes and audits nothing. The person
    concerned is told nothing (Q89).
    """
    kind = _known(Notice, notice, "notice")
    chosen = _known(Channel, channel, "channel")
    actor = authorize_in_txn(principal, Capability.ORG_MANAGE, locks=LockOrder())

    def execute() -> NoticeDefault:
        row, created = NoticeDefault.objects.select_for_update().get_or_create(
            notice=kind, channel=chosen, defaults={"enabled": enabled, "changed_by": actor}
        )
        if not created and row.enabled == enabled:
            return row
        before = True if created else row.enabled
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

    return execute_once(
        actor_id=principal.user_id,
        key=idempotency_key,
        operation=NOTICE_DEFAULT_OPERATION,
        payload={"notice": kind.value, "channel": chosen.value, "enabled": enabled},
        execute=execute,
        replay=lambda _type, pk: NoticeDefault.objects.get(pk=pk),
    )


def _clean_modes(modes: Mapping[Notice | str, Mode | str | None]) -> dict[Notice, Mode | None]:
    """``modes`` as members: a notice each, ``None`` (follow the setting for everyone) or a mode."""
    return {
        _known(Notice, notice, "modes"): None if mode in (None, "") else _known(Mode, mode, "modes")
        for notice, mode in modes.items()
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
    return actor, person


def _write_mode(
    actor: User,
    person: User,
    channel: Channel,
    notice: Notice,
    mode: Mode | None,
    row: NoticeSetting | None,
) -> None:
    """Make the person's row for ``notice`` say ``mode`` (``None``: no row)."""
    if mode is None:
        assert row is not None  # a person with no row and no mode differs from nothing
        row.delete()
    elif row is not None:
        row.mode, row.set_by, row.set_at = mode, actor, db_now()
        row.save(update_fields=["mode", "set_by", "set_at", "updated_at"])
    else:
        NoticeSetting.objects.create(
            user=person, notice=notice, channel=channel, mode=mode, set_by=actor
        )


def _apply_modes(
    actor: User, person: User, channel: Channel, modes: dict[Notice, Mode | None]
) -> dict[str, dict[str, str | None]]:
    """Write what differs from the person's rows (locked); return ``{notice: {before, after}}``."""
    rows = {
        row.notice: row
        for row in NoticeSetting.objects.select_for_update().filter(user=person, channel=channel)
    }
    changes: dict[str, dict[str, str | None]] = {}
    for notice, mode in modes.items():
        row = rows.get(notice)
        before = row.mode if row is not None else None
        if before == mode:
            continue
        _write_mode(actor, person, channel, notice, mode, row)
        changes[notice.value] = {"before": before, "after": mode.value if mode else None}
    return changes


@transactional
def set_person_notices(
    principal: Principal,
    user_id: UUID,
    modes: Mapping[Notice | str, Mode | str | None],
    *,
    channel: Channel | str = Channel.EMAIL,
    idempotency_key: str | None = None,
) -> User:
    """Set, for one person, which business notices they always or never receive (``org.manage``).

    ``modes`` names only the notices to change: ``Mode.ALWAYS``, ``Mode.NEVER`` or ``None``
    (follow the setting for everyone, which deletes the person's row). The person's own refusal of
    business email still wins over ``ALWAYS`` (Q87). The people an administrator reaches are all
    of them, but a staff or superuser account is not changed here and an unknown person is a 404.
    One audit event holds every notice that changed, with before and after; nothing is written or
    audited when nothing differs; the person is told nothing (Q89).
    """
    chosen = _known(Channel, channel, "channel")
    wanted = _clean_modes(modes)
    actor, person = _lock_person(principal, user_id)

    def execute() -> User:
        changes = _apply_modes(actor, person, chosen, wanted)
        if changes:
            record(
                principal.audit_identity,
                "notification.person_changed",
                obj=person,
                data={"channel": chosen.value, "changes": changes},
            )
            events.publish("notification.person_changed", user_id=person.pk)
        return person

    return execute_once(
        actor_id=principal.user_id,
        key=idempotency_key,
        operation=PERSON_NOTICES_OPERATION,
        payload={
            "user_id": user_id,
            "channel": chosen.value,
            "modes": {
                notice.value: mode.value if mode else None for notice, mode in wanted.items()
            },
        },
        execute=execute,
        replay=lambda _type, pk: User.objects.get(pk=pk),
    )

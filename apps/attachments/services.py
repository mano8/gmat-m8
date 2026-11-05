"""Uploading and removing attachments (plan §5; F14, F31, F45, F54, F73).

An upload goes through three phases:

1. **Admission** (``admit_upload``), in a short transaction, before any byte of the body is
   read (``uploads.ReservingUploadHandler``): ``authorize_in_txn`` with the parent's *add*
   capability, the parent header locked at its level, then ``StorageQuota`` (L8). It checks
   that used + reserved + the declared size fit the limit, that the parent has fewer than
   ``ATTACHMENT_MAX_PER_PARENT`` attachments (removed ones included) plus reservations in
   progress, and that fewer than ``ATTACHMENT_MAX_PROCESSING`` uploads with a live lease are
   in progress installation-wide; then it inserts the reservation (``PROCESSING``, with its
   lease) and commits.
2. **Processing** (``processing``), outside any transaction, under enforced limits: the
   body streams to the staging file and is cut above the reservation; the type is checked by
   its magic bytes; images are decoded and re-encoded in a killable child process.
3. **Finalization** (``finalize_upload``), in a short transaction: authorized again, the
   parent locked, then the quota and the reservation (L8). Only a reservation still
   ``PROCESSING`` with an unexpired lease is finalized. A stored file larger than its
   reservation takes the difference from the quota under the same lock, or is refused. On
   success the bytes move from reserved to used and the reservation is ``FINALIZED``; on
   any failure it is ``RELEASED`` (bytes returned, staged files deleted).

The worker releases expired leases (``housekeeping``). **Removal** (``remove_attachment``)
is a soft delete: by the uploader while the parent is a draft, otherwise by a holder of the
parent's *moderate* capability with a reason (``parents``). Additions, removals and the
80 % alert are audited on the parent.
"""

import logging
import unicodedata
from dataclasses import dataclass
from datetime import timedelta
from pathlib import PurePath
from typing import TYPE_CHECKING, Any
from uuid import UUID

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db.models import Model
from django.utils.translation import gettext_lazy as _

from apps.access.authorization import authorize_in_txn
from apps.access.principals import Principal
from apps.accounts.models import User
from apps.attachments import files, processing
from apps.attachments.models import Attachment, AttachmentReservation, StorageQuota
from apps.attachments.parents import ParentType, parent_of, parent_type, type_of
from apps.attachments.quota import (
    check_alert,
    headroom,
    lock_parent,
    lock_quota,
    lock_reservations,
    release,
)
from apps.audit.recorder import record
from apps.core import events
from apps.core.db import db_now
from apps.core.idempotency import execute_once
from apps.core.locking import LockOrder, run_in_transaction, transactional

if TYPE_CHECKING:
    from django.utils.functional import _StrPromise as Message


logger = logging.getLogger(__name__)

State = AttachmentReservation.State
ADD_OPERATION = "attachment.add"
NAME_LENGTH = 150

REFUSALS: dict[str, Message] = {
    "too_large": _("The file is larger than 10 MB."),
    "quota": _("The attachment storage is full. Ask an administrator."),
    "slots": _("This document already has the maximum number of attachments."),
    "busy": _("Other files are being processed. Try again in a moment."),
    "closed": _("Files can no longer be attached to this document."),
    "expired": _("The upload took too long. Try again."),
    "removed": _("This attachment was already removed."),
    **processing.REJECTIONS,
}


def refusal(code: str) -> ValidationError:
    return ValidationError(REFUSALS[code], code=code)


# --- rules ----------------------------------------------------------------------------


def _reservable(declared_bytes: int) -> int:
    """What an upload reserves: its declared length, at most one file's limit."""
    if declared_bytes > settings.ATTACHMENT_MAX_REQUEST_BYTES:
        raise refusal("too_large")
    if declared_bytes <= 0:
        raise refusal("empty")
    return min(declared_bytes, settings.ATTACHMENT_MAX_FILE_BYTES)


def _require_may_add(ptype: ParentType, parent: Model, actor: User) -> None:
    if not ptype.is_open(parent) or not ptype.may_add(parent, actor.pk):
        raise refusal("closed")


def _require_room(quota: StorageQuota, needed: int) -> None:
    if needed > headroom(quota):
        raise refusal("quota")


def _require_slot(ptype: ParentType, parent: Model) -> None:
    """Attachments of the parent (removed ones included) plus its uploads in progress."""
    link = {ptype.field: parent}
    used = Attachment.objects.filter(**link).count()
    used += AttachmentReservation.objects.filter(**link, state=State.PROCESSING).count()
    if used >= settings.ATTACHMENT_MAX_PER_PARENT:
        raise refusal("slots")


def _require_capacity(now: Any) -> None:
    """At most ``ATTACHMENT_MAX_PROCESSING`` live uploads (serialized by the quota lock)."""
    busy = AttachmentReservation.objects.filter(state=State.PROCESSING, lease_until__gt=now)
    if busy.count() >= settings.ATTACHMENT_MAX_PROCESSING:
        raise refusal("busy")


def _require_live(reservation: AttachmentReservation, actor: User, now: Any) -> None:
    if reservation.reserved_by_id != actor.pk:
        raise ValueError("A reservation is finalized by whoever holds it")
    if reservation.state != State.PROCESSING or reservation.lease_until <= now:
        raise refusal("expired")


def display_name(name: str, content_type: str) -> str:
    """The name the file is offered under: the sent name's stem, with the stored type's
    extension (images are re-encoded to WebP)."""
    stem = PurePath(unicodedata.normalize("NFC", name or "").replace("\\", "/")).stem
    stem = "".join(char for char in stem if char.isprintable() and char not in '"/:*?<>|')
    stem = stem.strip(" .")[: NAME_LENGTH - 5] or "attachment"
    return f"{stem}.{'pdf' if content_type == processing.PDF else 'webp'}"


# --- writing --------------------------------------------------------------------------


def _record(principal: Principal, action: str, parent: Model, **data: Any) -> None:
    branch_id = getattr(parent, "procurement_branch_id", None) or getattr(parent, "branch_id", None)
    record(
        principal.audit_identity,
        action,
        obj=parent,
        branch_id=branch_id,
        data={"number": getattr(parent, "number", ""), **data},
    )
    events.publish(action, parent_type=parent._meta.label_lower, parent_id=parent.pk)


def _attachment_data(attachment: Attachment) -> dict[str, Any]:
    return {
        "attachment_id": attachment.pk,
        "file_name": attachment.file_name,
        "content_type": attachment.content_type,
        "size_bytes": attachment.size_bytes,
        "sha256": attachment.sha256,
    }


def _store(processed: processing.Processed, storage_key: str) -> None:
    """Move the processed file to its stored name (again on a retried transaction)."""
    target = files.stored_path(storage_key)
    if processed.path.exists():
        files.ensure_dirs()
        processed.path.replace(target)
    elif not target.exists():
        raise FileNotFoundError("The processed file is gone")


def _settle_quota(principal: Principal, quota: StorageQuota, stored: int, reserved: int) -> None:
    quota.used_bytes += stored
    quota.reserved_bytes -= reserved
    quota.save(update_fields=["used_bytes", "reserved_bytes"])
    if check_alert(quota, db_now()):
        record(
            principal.audit_identity,
            "attachment.storage_alert",
            obj=quota,
            data={"used_bytes": quota.used_bytes, "limit_bytes": quota.limit_bytes},
        )


@dataclass(frozen=True)
class _Upload:
    reservation_id: UUID
    file_name: str
    storage_key: str
    idempotency_key: str | None


def _finalize(principal: Principal, upload: _Upload, processed: processing.Processed) -> Attachment:
    locks = LockOrder()
    reservation = AttachmentReservation.objects.get(pk=upload.reservation_id)
    parent = parent_of(reservation)
    ptype = type_of(parent)
    actor = authorize_in_txn(principal, ptype.add, parent, locks=locks, read=ptype.view)
    name = display_name(upload.file_name, processed.content_type)

    def held() -> tuple[Model, StorageQuota, AttachmentReservation]:
        locked = lock_parent(locks, ptype, parent)
        quota = lock_quota(locks)
        (row,) = lock_reservations(locks, [upload.reservation_id])
        _require_live(row, actor, db_now())
        return locked, quota, row

    def execute() -> Attachment:
        locked, quota, row = held()
        _require_may_add(ptype, locked, actor)
        _require_room(quota, processed.size - row.reserved_bytes)
        _store(processed, upload.storage_key)
        attachment = Attachment.objects.create(
            **{ptype.field: locked},
            storage_key=upload.storage_key,
            file_name=name,
            content_type=processed.content_type,
            size_bytes=processed.size,
            sha256=processed.sha256,
            uploaded_by=actor,
            uploaded_at=db_now(),
        )
        _settle_quota(principal, quota, processed.size, row.reserved_bytes)
        row.state, row.finished_at = State.FINALIZED, db_now()
        row.save(update_fields=["state", "finished_at", "updated_at"])
        _record(principal, "attachment.added", locked, **_attachment_data(attachment))
        return attachment

    def replay(_type: str, pk: UUID) -> Attachment:
        """A double tap: the first upload stored the file; this one gives its slot back."""
        _, quota, row = held()
        release(quota, row, db_now())
        return Attachment.objects.get(pk=pk)

    return execute_once(
        actor_id=principal.user_id,
        key=upload.idempotency_key,
        operation=ADD_OPERATION,
        payload={
            "parent": ptype.kind,
            "parent_id": parent.pk,
            "name": name,
            "sha": processed.sha256,
        },
        execute=execute,
        replay=replay,
    )


def _audit_malware(principal: Principal, reservation_id: UUID, signature: str) -> None:
    """On the document it was meant for. The refusal stands if this fails: it is the log's too."""

    def execute() -> None:
        parent = parent_of(AttachmentReservation.objects.get(pk=reservation_id))
        _record(principal, "attachment.malware_refused", parent, signature=signature)

    try:
        run_in_transaction(execute)
    except Exception:
        logger.warning("The refused file on %s was not audited", reservation_id, exc_info=True)


def _release_after_failure(principal: Principal, reservation_id: UUID) -> None:
    """Give the slot back now; if even that fails, the lease expiry does it (worker)."""
    try:
        release_reservation(principal, reservation_id)
    except Exception:  # the original error is what the user must see
        logger.warning("Reservation %s left to its lease expiry", reservation_id, exc_info=True)


# --- services -------------------------------------------------------------------------


@transactional
def admit_upload(
    principal: Principal, kind: str, parent_id: UUID, *, declared_bytes: int
) -> AttachmentReservation:
    """Phase 1: reserve a slot and ``declared_bytes`` for one file on the parent."""
    ptype = parent_type(kind)
    reserved = _reservable(declared_bytes)
    locks = LockOrder()
    parent = ptype.model._default_manager.get(pk=parent_id)
    actor = authorize_in_txn(principal, ptype.add, parent, locks=locks, read=ptype.view)
    locked = lock_parent(locks, ptype, parent)
    _require_may_add(ptype, locked, actor)
    quota = lock_quota(locks)
    now = db_now()
    _require_room(quota, reserved)
    _require_slot(ptype, locked)
    _require_capacity(now)
    reservation = AttachmentReservation.objects.create(
        **{ptype.field: locked},
        reserved_by=actor,
        reserved_bytes=reserved,
        lease_until=now + timedelta(seconds=settings.ATTACHMENT_LEASE),
    )
    quota.reserved_bytes += reserved
    quota.save(update_fields=["reserved_bytes"])
    return reservation


def finalize_upload(
    principal: Principal,
    reservation_id: UUID,
    *,
    file_name: str,
    idempotency_key: str | None = None,
) -> Attachment:
    """Phases 2 and 3: process the staged file of ``reservation_id`` and store it.

    Any failure releases the reservation and deletes what was staged or stored, then
    raises (``ValidationError`` with a ``REFUSALS`` code for a refused file).
    """
    upload = _Upload(reservation_id, file_name, files.new_storage_key(), idempotency_key)
    source, target = files.staged_input(reservation_id), files.staged_output(reservation_id)
    try:
        processed = processing.process(source, target)
        return run_in_transaction(lambda: _finalize(principal, upload, processed))
    except processing.Rejected as exc:
        if exc.signature:
            _audit_malware(principal, reservation_id, exc.signature)
        _release_after_failure(principal, reservation_id)
        raise refusal(exc.code) from exc
    except BaseException:
        files.discard(files.stored_path(upload.storage_key))
        _release_after_failure(principal, reservation_id)
        raise
    finally:
        files.discard_staged(reservation_id)


@transactional
def release_reservation(principal: Principal, reservation_id: UUID) -> bool:
    """Give back the slot and bytes of the caller's own upload still in progress.

    For an upload that failed before finalization (a CSRF refusal, a broken body). Returns
    whether anything was released.
    """
    found = AttachmentReservation.objects.filter(pk=reservation_id).first()
    if found is None:
        return False
    parent = parent_of(found)
    ptype = type_of(parent)
    locks = LockOrder()
    actor = authorize_in_txn(principal, ptype.view, parent, locks=locks)
    if found.reserved_by_id != actor.pk:
        return False
    lock_parent(locks, ptype, parent)
    quota = lock_quota(locks)
    (row,) = lock_reservations(locks, [reservation_id])
    if row.state != State.PROCESSING:
        return False
    release(quota, row, db_now())
    return True


def _removal_reason(text: str) -> str:
    text = text.strip()
    if not text:
        raise ValidationError({"reason": ValidationError(_("Give a reason."), code="required")})
    return text


def _own_draft(ptype: ParentType, parent: Model, attachment: Attachment, user_id: UUID) -> bool:
    return attachment.uploaded_by_id == user_id and ptype.is_draft(parent)


@transactional
def remove_attachment(principal: Principal, attachment_id: UUID, *, reason: str = "") -> Attachment:
    """Soft-delete an attachment: the uploader on a draft, else a moderator with a reason.

    The file stays (and counts against the quota) until the retention purge.
    """
    attachment = Attachment.objects.get(pk=attachment_id)
    parent = parent_of(attachment)
    ptype = type_of(parent)
    own = _own_draft(ptype, parent, attachment, principal.user_id)
    capability = ptype.add if own else ptype.moderate
    locks = LockOrder()
    actor = authorize_in_txn(principal, capability, parent, locks=locks, read=ptype.view)
    # A request that left its draft since the check above fails the verification here, and
    # the retry decides again who may remove (``ParentType.verified``).
    locked = lock_parent(locks, ptype, parent)
    reason = reason.strip() if own else _removal_reason(reason)
    row = Attachment.objects.get(pk=attachment_id)
    if row.removed_at is not None:
        raise refusal("removed")
    row.removed_at, row.removed_by, row.removal_reason = db_now(), actor, reason
    row.save(update_fields=["removed_at", "removed_by", "removal_reason", "updated_at"])
    _record(principal, "attachment.removed", locked, **_attachment_data(row), reason=reason)
    return row

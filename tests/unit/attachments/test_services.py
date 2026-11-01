"""The attachment services (plan §5; F14, F31, F45): admission with its three limits,
finalization (lease, extra quota, idempotency), release, removal, and the rows' immutability.

The concurrent cases are in ``tests/integration/test_attachment_races.py``; who may call what
in ``test_matrix``.
"""

from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.utils import timezone

from apps.access.exceptions import AccessDenied, NotInScope
from apps.access.principals import SessionPrincipal
from apps.accounts.models import User
from apps.attachments import files, services
from apps.attachments.models import (
    QUOTA_ID,
    Attachment,
    AttachmentReservation,
    StorageQuota,
)
from apps.attachments.processing import Processed
from apps.audit.models import AuditEvent
from apps.core.idempotency import IdempotencyConflict, issue_key
from apps.handovers.models import Handover
from apps.material_requests.models import MaterialRequest
from apps.receiving.models import GoodsReceipt
from tests.org import People
from tests.support import error_codes, principal_for
from tests.unit.attachments.conftest import PDF_BYTES, Attach, admit, png, stage, submit

pytestmark = pytest.mark.django_db

State = AttachmentReservation.State
MB = 1024 * 1024


def quota() -> StorageQuota:
    return StorageQuota.objects.get(pk=QUOTA_ID)


def codes(caught: Any) -> set[str]:
    return error_codes(caught)


class TestAdmission:
    def test_a_reservation_holds_a_slot_and_its_bytes(
        self, draft: MaterialRequest, tech: SessionPrincipal
    ) -> None:
        reservation = admit(tech, draft, 5000)
        assert reservation.state == State.PROCESSING
        assert reservation.request == draft
        assert reservation.reserved_bytes == 5000
        assert reservation.lease_until > timezone.now() + timedelta(minutes=14)
        assert quota().reserved_bytes == 5000

    def test_a_declared_length_above_one_file_reserves_one_file(
        self, draft: MaterialRequest, tech: SessionPrincipal
    ) -> None:
        assert admit(tech, draft, 10 * MB + 1000).reserved_bytes == 10 * MB

    @pytest.mark.parametrize(
        ("size", "code"), [(10 * MB + 64 * 1024 + 1, "too_large"), (0, "empty")]
    )
    def test_a_declared_length_out_of_bounds_is_refused(
        self, draft: MaterialRequest, tech: SessionPrincipal, size: int, code: str
    ) -> None:
        with pytest.raises(ValidationError) as caught:
            admit(tech, draft, size)
        assert codes(caught) == {code}

    def test_a_file_beyond_the_quota_is_rejected(
        self, settings: Any, draft: MaterialRequest, tech: SessionPrincipal
    ) -> None:
        settings.ATTACHMENT_STORAGE_LIMIT = 10_000
        admit(tech, draft, 6000)
        with pytest.raises(ValidationError) as caught:
            admit(tech, draft, 5000)
        assert codes(caught) == {"quota"}
        assert quota().limit_bytes == 10_000  # the setting is the authority on the limit
        assert quota().reserved_bytes == 6000

    def test_a_parent_takes_ten_files_removed_ones_included(
        self,
        settings: Any,
        draft: MaterialRequest,
        tech: SessionPrincipal,
        attach: Attach,
    ) -> None:
        settings.ATTACHMENT_MAX_PROCESSING = 20
        first = attach(tech, draft)
        services.remove_attachment(tech, first.pk)
        for _ in range(9):  # one removed file and nine uploads in progress make ten
            admit(tech, draft, 100)
        with pytest.raises(ValidationError) as caught:
            admit(tech, draft, 100)
        assert codes(caught) == {"slots"}

    def test_a_third_upload_in_progress_is_busy(
        self, draft: MaterialRequest, receipt: GoodsReceipt, tech: SessionPrincipal, mm: Any
    ) -> None:
        admit(tech, draft, 100)
        admit(mm, receipt, 100)
        with pytest.raises(ValidationError) as caught:
            admit(mm, receipt, 100)
        assert codes(caught) == {"busy"}

    def test_an_expired_lease_no_longer_counts_as_busy(
        self, draft: MaterialRequest, tech: SessionPrincipal
    ) -> None:
        for _ in range(2):
            admit(tech, draft, 100)
        AttachmentReservation.objects.update(lease_until=timezone.now() - timedelta(seconds=1))
        assert admit(tech, draft, 100).state == State.PROCESSING

    def test_only_the_requester_adds_and_only_to_a_draft(
        self, draft: MaterialRequest, people: People, mm: Any
    ) -> None:
        with pytest.raises(AccessDenied):
            admit(mm, draft, 100)  # sees the request, holds no request.create on it
        submit(draft)
        with pytest.raises(ValidationError) as caught:
            admit(principal_for(people.tech_mad), draft, 100)
        assert codes(caught) == {"closed"}

    def test_a_voided_receipt_takes_no_file(self, receipt: GoodsReceipt, mm: Any) -> None:
        GoodsReceipt.objects.filter(pk=receipt.pk).update(
            status=GoodsReceipt.Status.VOIDED,
            voided_at=timezone.now(),
            voided_by=receipt.received_by,
            void_reason="wrong",
        )
        with pytest.raises(ValidationError) as caught:
            admit(mm, receipt, 100)
        assert codes(caught) == {"closed"}

    def test_out_of_scope_is_not_found(self, receipt: GoodsReceipt, people: People) -> None:
        with pytest.raises(NotInScope):
            admit(principal_for(people.mm_sev), receipt, 100)

    def test_a_site_manager_may_not_add_to_a_handover(
        self, handover: Handover, people: People
    ) -> None:
        with pytest.raises(AccessDenied):
            admit(principal_for(people.site_manager), handover, 100)

    def test_the_quota_row_is_recreated_after_a_flush(
        self, draft: MaterialRequest, tech: SessionPrincipal
    ) -> None:
        StorageQuota.objects.all().delete()
        admit(tech, draft, 100)
        assert quota().reserved_bytes == 100


class TestFinalization:
    def test_a_photo_is_stored(
        self,
        media: Path,
        draft: MaterialRequest,
        tech: SessionPrincipal,
        attach: Attach,
        people: People,
    ) -> None:
        item = attach(tech, draft, png(), name="C:\\fotos\\Albarán 12.JPG", declared=4000)
        assert item.request == draft
        assert (item.file_name, item.content_type) == ("Albarán 12.webp", "image/webp")
        assert item.uploaded_by == people.tech_mad
        stored = files.stored_path(item.storage_key)
        assert stored.read_bytes()[:4] == b"RIFF"
        assert stored.stat().st_size == item.size_bytes
        assert files.sha256_of(stored) == item.sha256
        assert list(files.staging_dir().iterdir()) == []
        assert (quota().used_bytes, quota().reserved_bytes) == (item.size_bytes, 0)
        reservation = AttachmentReservation.objects.get()
        assert reservation.state == State.FINALIZED
        assert reservation.finished_at is not None
        event = AuditEvent.objects.get(action="attachment.added")
        assert (event.object_id, event.actor_id) == (draft.pk, people.tech_mad.pk)
        assert event.data["sha256"] == item.sha256
        assert event.branch_id == draft.procurement_branch_id

    def test_a_pdf_keeps_its_bytes(self, receipt: GoodsReceipt, mm: Any, attach: Attach) -> None:
        item = attach(mm, receipt, PDF_BYTES, name="albaran.pdf")
        assert item.file_name == "albaran.pdf"
        assert files.stored_path(item.storage_key).read_bytes() == PDF_BYTES
        event = AuditEvent.objects.get(action="attachment.added")
        assert (event.object_type, event.branch_id) == ("receiving.goodsreceipt", receipt.branch_id)

    def test_a_rejected_file_releases_its_reservation(
        self, media: Path, draft: MaterialRequest, tech: SessionPrincipal, attach: Attach
    ) -> None:
        with pytest.raises(ValidationError) as caught:
            attach(tech, draft, b"<svg xmlns='http://www.w3.org/2000/svg'/>", name="x.jpg")
        assert codes(caught) == {"type"}
        assert AttachmentReservation.objects.get().state == State.RELEASED
        assert (quota().used_bytes, quota().reserved_bytes) == (0, 0)
        assert not Attachment.objects.exists()
        assert list(files.staging_dir().iterdir()) == []

    def test_finalizing_an_expired_reservation_fails(
        self, draft: MaterialRequest, tech: SessionPrincipal
    ) -> None:
        reservation = admit(tech, draft, 5000)
        stage(reservation, png())
        AttachmentReservation.objects.update(lease_until=timezone.now() - timedelta(seconds=1))
        with pytest.raises(ValidationError) as caught:
            services.finalize_upload(tech, reservation.pk, file_name="a.png")
        assert codes(caught) == {"expired"}
        assert AttachmentReservation.objects.get().state == State.RELEASED
        assert quota().reserved_bytes == 0
        assert not Attachment.objects.exists()
        assert not any(files.files_dir().iterdir())

    def test_finalizing_a_released_reservation_fails(
        self, draft: MaterialRequest, tech: SessionPrincipal
    ) -> None:
        reservation = admit(tech, draft, 5000)
        assert services.release_reservation(tech, reservation.pk)
        stage(reservation, png())
        with pytest.raises(ValidationError) as caught:
            services.finalize_upload(tech, reservation.pk, file_name="a.png")
        assert codes(caught) == {"expired"}
        assert quota().reserved_bytes == 0  # released once, not twice

    def test_an_output_larger_than_its_reservation_takes_extra_quota(
        self, draft: MaterialRequest, tech: SessionPrincipal
    ) -> None:
        reservation = admit(tech, draft, 10)  # a tiny declared length
        stage(reservation, PDF_BYTES)
        item = services.finalize_upload(tech, reservation.pk, file_name="a.pdf")
        assert item.size_bytes > 10
        assert (quota().used_bytes, quota().reserved_bytes) == (item.size_bytes, 0)

    def test_an_output_larger_than_its_reservation_without_room_is_rejected(
        self, settings: Any, draft: MaterialRequest, tech: SessionPrincipal
    ) -> None:
        settings.ATTACHMENT_STORAGE_LIMIT = len(PDF_BYTES) - 1
        reservation = admit(tech, draft, 10)
        stage(reservation, PDF_BYTES)
        with pytest.raises(ValidationError) as caught:
            services.finalize_upload(tech, reservation.pk, file_name="a.pdf")
        assert codes(caught) == {"quota"}
        assert AttachmentReservation.objects.get().state == State.RELEASED
        assert (quota().used_bytes, quota().reserved_bytes) == (0, 0)
        assert not any(files.files_dir().iterdir())

    def test_the_parent_closing_meanwhile_refuses_the_file(
        self, draft: MaterialRequest, tech: SessionPrincipal
    ) -> None:
        reservation = admit(tech, draft, 5000)
        stage(reservation, png())
        submit(draft)
        with pytest.raises(ValidationError) as caught:
            services.finalize_upload(tech, reservation.pk, file_name="a.png")
        assert codes(caught) == {"closed"}
        assert AttachmentReservation.objects.get().state == State.RELEASED

    def test_only_the_holder_finalizes(
        self, draft: MaterialRequest, tech: SessionPrincipal, people: People
    ) -> None:
        reservation = admit(tech, draft, 5000)
        stage(reservation, png())
        with pytest.raises(AccessDenied):
            services.finalize_upload(principal_for(people.mm_mad), reservation.pk, file_name="a")
        assert AttachmentReservation.objects.get().state == State.PROCESSING

    def test_a_replay_returns_the_first_file_and_releases_its_own_slot(
        self, draft: MaterialRequest, tech: SessionPrincipal, attach: Attach, people: People
    ) -> None:
        key = issue_key(people.tech_mad.pk, services.ADD_OPERATION)
        first = attach(tech, draft, png(), key=key)
        again = attach(tech, draft, png(), key=key)
        assert again == first
        assert Attachment.objects.count() == 1
        assert sorted(AttachmentReservation.objects.values_list("state", flat=True)) == [
            State.FINALIZED,
            State.RELEASED,
        ]
        assert (quota().used_bytes, quota().reserved_bytes) == (first.size_bytes, 0)

    def test_the_same_key_with_another_file_conflicts(
        self, draft: MaterialRequest, tech: SessionPrincipal, attach: Attach, people: People
    ) -> None:
        key = issue_key(people.tech_mad.pk, services.ADD_OPERATION)
        attach(tech, draft, png(), key=key)
        with pytest.raises(IdempotencyConflict):
            attach(tech, draft, PDF_BYTES, key=key)
        assert AttachmentReservation.objects.filter(state=State.RELEASED).count() == 1
        assert quota().reserved_bytes == 0

    def test_crossing_80_percent_raises_the_alert_once(
        self, settings: Any, draft: MaterialRequest, tech: SessionPrincipal, attach: Attach
    ) -> None:
        settings.ATTACHMENT_STORAGE_LIMIT = 500  # 80 %: the sixth PDF
        attach(tech, draft, PDF_BYTES, declared=len(PDF_BYTES))
        assert quota().alert_raised_at is None
        for _ in range(9):
            try:
                attach(tech, draft, PDF_BYTES, declared=len(PDF_BYTES))
            except ValidationError:
                break
        assert quota().alert_raised_at is not None
        assert AuditEvent.objects.filter(action="attachment.storage_alert").count() == 1


class TestRelease:
    def test_the_holder_releases_once(self, draft: MaterialRequest, tech: SessionPrincipal) -> None:
        reservation = admit(tech, draft, 5000)
        stage(reservation, b"partial")
        assert services.release_reservation(tech, reservation.pk)
        assert not services.release_reservation(tech, reservation.pk)
        assert quota().reserved_bytes == 0
        assert not files.staged_input(reservation.pk).exists()

    def test_someone_else_releases_nothing(
        self, receipt: GoodsReceipt, mm: Any, people: People
    ) -> None:
        reservation = admit(mm, receipt, 5000)
        assert not services.release_reservation(principal_for(people.wo_mad), reservation.pk)
        assert AttachmentReservation.objects.get().state == State.PROCESSING

    def test_an_unknown_reservation_releases_nothing(self, tech: SessionPrincipal) -> None:
        assert not services.release_reservation(tech, QUOTA_ID)


class TestRemoval:
    def test_the_uploader_removes_from_a_draft_without_a_reason(
        self, draft: MaterialRequest, tech: SessionPrincipal, attach: Attach, people: People
    ) -> None:
        item = attach(tech, draft)
        removed = services.remove_attachment(tech, item.pk)
        assert removed.removed_by == people.tech_mad
        assert removed.removal_reason == ""
        assert files.stored_path(item.storage_key).exists()  # until the purge
        assert quota().used_bytes == item.size_bytes
        event = AuditEvent.objects.get(action="attachment.removed")
        assert event.data["attachment_id"] == str(item.pk)

    def test_after_submission_only_a_moderator_removes_with_a_reason(
        self, draft: MaterialRequest, tech: SessionPrincipal, attach: Attach, mm: Any
    ) -> None:
        item = attach(tech, draft)
        submit(draft)
        with pytest.raises(AccessDenied):
            services.remove_attachment(tech, item.pk)
        with pytest.raises(ValidationError) as caught:
            services.remove_attachment(mm, item.pk, reason="  ")
        assert codes(caught) == {"required"}
        removed = services.remove_attachment(mm, item.pk, reason="Wrong photo")
        assert removed.removal_reason == "Wrong photo"

    def test_a_receipt_file_is_removed_by_a_manager_only(
        self, receipt: GoodsReceipt, mm: Any, attach: Attach, people: People
    ) -> None:
        item = attach(mm, receipt)
        with pytest.raises(AccessDenied):
            services.remove_attachment(principal_for(people.wo_mad), item.pk, reason="x")
        services.remove_attachment(mm, item.pk, reason="Duplicate")
        with pytest.raises(ValidationError) as caught:
            services.remove_attachment(mm, item.pk, reason="Again")
        assert codes(caught) == {"removed"}


class TestImmutability:
    def test_an_attachment_is_never_relinked(
        self, draft: MaterialRequest, receipt: GoodsReceipt, tech: SessionPrincipal, attach: Attach
    ) -> None:
        item = attach(tech, draft)
        for change in ({"request": None, "receipt": receipt}, {"file_name": "other.webp"}):
            with pytest.raises(IntegrityError), transaction.atomic():
                Attachment.objects.filter(pk=item.pk).update(**change)

    def test_a_removal_is_final_and_only_a_removed_row_is_deleted(
        self, draft: MaterialRequest, tech: SessionPrincipal, attach: Attach
    ) -> None:
        item = attach(tech, draft)
        with pytest.raises(IntegrityError), transaction.atomic():
            Attachment.objects.filter(pk=item.pk).delete()
        services.remove_attachment(tech, item.pk)
        with pytest.raises(IntegrityError), transaction.atomic():
            Attachment.objects.filter(pk=item.pk).update(removal_reason="changed")
        Attachment.objects.filter(pk=item.pk).delete()

    def test_exactly_one_parent(self, draft: MaterialRequest, receipt: GoodsReceipt) -> None:
        common = {
            "storage_key": files.new_storage_key(),
            "file_name": "a.pdf",
            "content_type": "application/pdf",
            "size_bytes": 1,
            "sha256": "0" * 64,
            "uploaded_by": draft.requested_by,
            "uploaded_at": timezone.now(),
        }
        for parents in ({}, {"request": draft, "receipt": receipt}):
            with pytest.raises(IntegrityError), transaction.atomic():
                Attachment.objects.create(**parents, **common)


def test_display_names_are_safe() -> None:
    assert services.display_name('../../etc/<pass>"wd".png', "image/webp") == "passwd.webp"
    assert services.display_name("", "application/pdf") == "attachment.pdf"
    assert services.display_name("x" * 400 + ".pdf", "application/pdf") == "x" * 145 + ".pdf"
    assert services.display_name("..", "image/webp") == "attachment.webp"


class TestFailurePaths:
    def test_another_poster_cannot_finalize_someone_elses_upload(
        self, receipt: GoodsReceipt, mm: Any, people: People
    ) -> None:
        reservation = admit(principal_for(people.wo_mad), receipt, 5000)
        stage(reservation, png())
        with pytest.raises(ValueError, match="whoever holds it"):
            services.finalize_upload(mm, reservation.pk, file_name="a.png")
        # Nor may they release it: it stays with its holder until its lease ends.
        assert AttachmentReservation.objects.get().state == State.PROCESSING

    def test_an_upload_of_a_deactivated_user_is_left_to_its_lease(
        self, draft: MaterialRequest, tech: SessionPrincipal, people: People
    ) -> None:
        reservation = admit(tech, draft, 5000)
        stage(reservation, png())
        User.objects.filter(pk=people.tech_mad.pk).update(is_active=False)
        with pytest.raises(AccessDenied):
            services.finalize_upload(tech, reservation.pk, file_name="a.png")
        assert AttachmentReservation.objects.get().state == State.PROCESSING
        assert not files.staged_input(reservation.pk).exists()
        assert not any(files.files_dir().iterdir())

    def test_a_retried_finalization_finds_the_file_already_stored(self, media: Path) -> None:
        files.ensure_dirs()
        gone = media / "staging" / "gone.out"
        key = files.new_storage_key()
        processed = Processed(gone, "application/pdf", 3, "0" * 64)
        with pytest.raises(FileNotFoundError):
            services._store(processed, key)
        files.stored_path(key).write_bytes(b"pdf")  # moved by the first attempt
        services._store(processed, key)
        assert files.stored_path(key).read_bytes() == b"pdf"

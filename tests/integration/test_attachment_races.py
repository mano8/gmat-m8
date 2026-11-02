"""Attachment admission and finalization under real concurrency (plan §5; F31, F45; M6.4
done-when).

Every admission locks the quota row (L8), so concurrent uploads are serialized at the one
point where the limits are checked:

- uploads to different documents near the storage limit never push used + reserved past it;
- uploads to one document never hold more than ten slots;
- the uploads in processing never exceed two at any instant (a sampler watches while slow
  decoders run), and every other one is refused as busy;
- a finalization racing the worker's lease expiry: exactly one of them wins, and the bytes
  are counted once.
"""

import threading
import time
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
from django.db import connections
from django.utils import timezone

from apps.access.principals import SessionPrincipal
from apps.attachments import files, housekeeping, processing, services
from apps.attachments.models import (
    QUOTA_ID,
    Attachment,
    AttachmentReservation,
    StorageQuota,
)
from apps.material_requests.models import MaterialRequest
from tests.integration.conftest import run_concurrently
from tests.org import Org, People
from tests.support import error_codes, principal_for
from tests.unit.attachments.conftest import png

pytestmark = [pytest.mark.integration, pytest.mark.django_db(transaction=True)]

State = AttachmentReservation.State
PDF = b"%PDF-1.4\n" + b"%" * 990 + b"\n%%EOF\n"  # 1000 bytes, stored as they are


@pytest.fixture(autouse=True)
def media(settings: Any, tmp_path: Path) -> Path:
    settings.ATTACHMENT_ROOT = str(tmp_path / "media")
    return tmp_path / "media"


@pytest.fixture
def tech(people: People) -> SessionPrincipal:
    return principal_for(people.tech_mad)


def drafts(org: Org, people: People, count: int) -> list[MaterialRequest]:
    return [
        MaterialRequest.objects.create(
            project=org.hospital, procurement_branch=org.a_mad, requested_by=people.tech_mad
        )
        for _ in range(count)
    ]


def upload(principal: SessionPrincipal, parent: MaterialRequest, data: bytes) -> Attachment:
    reservation = services.admit_upload(principal, "request", parent.pk, declared_bytes=len(data))
    files.ensure_dirs()
    files.staged_input(reservation.pk).write_bytes(data)
    return services.finalize_upload(principal, reservation.pk, file_name="a.pdf")


def quota() -> StorageQuota:
    return StorageQuota.objects.get(pk=QUOTA_ID)


def refusals(results: list[Any]) -> list[set[str]]:
    return [error_codes(result) for result in results if isinstance(result, Exception)]


def test_uploads_to_different_documents_never_pass_the_storage_limit(
    settings: Any, org: Org, people: People, tech: SessionPrincipal
) -> None:
    settings.ATTACHMENT_STORAGE_LIMIT = 3 * len(PDF) + 500
    settings.ATTACHMENT_MAX_PROCESSING = 20
    parents = drafts(org, people, 8)
    results = run_concurrently(8, lambda index: upload(tech, parents[index], PDF))
    stored = [result for result in results if isinstance(result, Attachment)]
    assert len(stored) == 3
    assert refusals(results) == [{"quota"}] * 5
    assert (quota().used_bytes, quota().reserved_bytes) == (3 * len(PDF), 0)
    assert quota().used_bytes <= settings.ATTACHMENT_STORAGE_LIMIT


def test_one_document_never_holds_more_than_ten_slots(
    settings: Any, org: Org, people: People, tech: SessionPrincipal
) -> None:
    settings.ATTACHMENT_MAX_PROCESSING = 20
    (parent,) = drafts(org, people, 1)
    results = run_concurrently(
        14,
        lambda index: services.admit_upload(tech, "request", parent.pk, declared_bytes=100),
    )
    admitted = [result for result in results if isinstance(result, AttachmentReservation)]
    assert len(admitted) == 10
    assert refusals(results) == [{"slots"}] * 4
    assert quota().reserved_bytes == 1000


def test_never_more_than_two_uploads_in_processing(
    org: Org, people: People, tech: SessionPrincipal, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Slow decoders keep two uploads in processing while four more try to start."""
    parents = drafts(org, people, 6)
    real = processing.decoder_command

    def slow(*args: Any) -> list[str]:
        command = real(*args)
        return [command[0], "-c", "import time, runpy, sys; time.sleep(1); "
                "sys.argv = sys.argv[1:]; runpy.run_path(sys.argv[0], run_name='__main__')",
                *command[2:]]  # fmt: skip

    monkeypatch.setattr(processing, "decoder_command", slow)
    peak, stop = [0], threading.Event()

    def sample() -> None:
        try:
            while not stop.is_set():
                live = AttachmentReservation.objects.filter(
                    state=State.PROCESSING, lease_until__gt=timezone.now()
                ).count()
                peak[0] = max(peak[0], live)
                time.sleep(0.01)
        finally:
            connections.close_all()

    sampler = threading.Thread(target=sample)
    sampler.start()
    image = png(16, 16)
    try:
        results = run_concurrently(6, lambda index: upload(tech, parents[index], image))
    finally:
        stop.set()
        sampler.join(10)
    assert peak[0] <= 2
    assert sum(isinstance(result, Attachment) for result in results) == 2
    assert refusals(results) == [{"busy"}] * 4
    assert quota().reserved_bytes == 0


@pytest.mark.parametrize("attempt", range(3))
def test_finalization_and_expiry_never_both_win(
    org: Org, people: People, tech: SessionPrincipal, attempt: int
) -> None:
    (parent,) = drafts(org, people, 1)
    reservation = services.admit_upload(tech, "request", parent.pk, declared_bytes=len(PDF))
    files.ensure_dirs()
    files.staged_input(reservation.pk).write_bytes(PDF)
    # The lease runs out while the file is being finalized.
    AttachmentReservation.objects.filter(pk=reservation.pk).update(
        lease_until=timezone.now() + timedelta(milliseconds=50 * attempt)
    )

    def work(index: int) -> Any:
        if index == 0:
            return services.finalize_upload(tech, reservation.pk, file_name="a.pdf")
        time.sleep(0.05 * attempt)
        return housekeeping.release_expired_reservations()

    results = run_concurrently(2, work)
    reservation.refresh_from_db()
    if reservation.state == State.FINALIZED:
        assert isinstance(results[0], Attachment)
        assert (quota().used_bytes, quota().reserved_bytes) == (len(PDF), 0)
    else:
        assert reservation.state == State.RELEASED
        assert error_codes(results[0]) == {"expired"}
        assert not Attachment.objects.exists()
        assert (quota().used_bytes, quota().reserved_bytes) == (0, 0)

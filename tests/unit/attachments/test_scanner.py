"""Every upload is scanned before it is stored, and no verdict means no file (M8.3, ADR-0015)."""

import tempfile
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from apps.attachments import processing, scanner
from apps.attachments.models import Attachment, AttachmentReservation
from apps.audit.models import AuditEvent
from apps.material_requests.models import MaterialRequest
from tests.clamd import MARKER, SIGNATURE, Behaviour, fake_clamd, refused_socket
from tests.org import People
from tests.org import client_for as signed_in
from tests.unit.attachments.conftest import PDF_BYTES, Attach, png
from tests.unit.attachments.test_views import send

MALWARE_PDF = PDF_BYTES + MARKER


@pytest.fixture
def clamd(settings: Any) -> Iterator[Behaviour]:
    with fake_clamd() as (address, behaviour):
        settings.ATTACHMENT_SCAN_SOCKET = address
        yield behaviour


def write(tmp_path: Path, data: bytes = PDF_BYTES) -> Path:
    path = tmp_path / "file"
    path.write_bytes(data)
    return path


class TestClient:
    def test_nothing_is_asked_without_a_socket(self, tmp_path: Path, settings: Any) -> None:
        settings.ATTACHMENT_SCAN_SOCKET = ""
        scanner.scan(write(tmp_path, MALWARE_PDF))
        scanner.ping()

    def test_a_clean_file_passes_whole(self, tmp_path: Path, clamd: Behaviour) -> None:
        data = b"x" * (scanner.CHUNK * 2 + 17)
        scanner.scan(write(tmp_path, data))
        assert clamd.received == [data]

    def test_a_found_signature_is_named(self, tmp_path: Path, clamd: Behaviour) -> None:
        with pytest.raises(scanner.Infected) as found:
            scanner.scan(write(tmp_path, MALWARE_PDF))
        assert found.value.signature == SIGNATURE

    @pytest.mark.parametrize(
        "reply",
        [
            b"INSTREAM size limit exceeded. ERROR\0",
            b"",  # the connection closed without a verdict
            b"stream: something else\0",
        ],
    )
    def test_anything_else_is_no_verdict(
        self, tmp_path: Path, clamd: Behaviour, reply: bytes
    ) -> None:
        clamd.reply = reply
        with pytest.raises(scanner.ScannerUnavailable):
            scanner.scan(write(tmp_path))

    def test_nothing_listening_is_no_verdict(self, tmp_path: Path, settings: Any) -> None:
        settings.ATTACHMENT_SCAN_SOCKET = refused_socket(tmp_path)
        with pytest.raises(scanner.ScannerUnavailable):
            scanner.scan(write(tmp_path))
        with pytest.raises(scanner.ScannerUnavailable):
            scanner.ping()

    def test_a_scan_that_takes_too_long_is_no_verdict(
        self, tmp_path: Path, clamd: Behaviour, settings: Any
    ) -> None:
        clamd.silent = True
        settings.ATTACHMENT_SCAN_TIMEOUT = 0.2
        with pytest.raises(scanner.ScannerUnavailable):
            scanner.scan(write(tmp_path))

    def test_a_deadline_already_over_is_no_verdict(self, tmp_path: Path, clamd: Any) -> None:
        with pytest.raises(scanner.ScannerUnavailable, match="too long"):
            scanner._left(0.0)

    def test_ping(self, clamd: Behaviour) -> None:
        scanner.ping()
        clamd.reply = b"NOPE\0"
        with pytest.raises(scanner.ScannerUnavailable):
            scanner.ping()


class TestProcessing:
    def test_the_scan_sees_the_original_bytes_of_an_image(
        self, tmp_path: Path, clamd: Behaviour
    ) -> None:
        """Before the decoder parses it, and not the re-encoded copy."""
        original = png()
        done = processing.process(write(tmp_path, original), tmp_path / "out")
        assert clamd.received == [original]
        assert done.content_type == processing.WEBP

    def test_a_pdf_is_scanned(self, tmp_path: Path, clamd: Behaviour) -> None:
        done = processing.process(write(tmp_path), tmp_path / "out")
        assert clamd.received == [PDF_BYTES]
        assert done.content_type == processing.PDF

    def test_a_refused_type_never_reaches_the_scanner(
        self, tmp_path: Path, clamd: Behaviour
    ) -> None:
        with pytest.raises(processing.Rejected) as refused:
            processing.process(write(tmp_path, b"<html></html>"), tmp_path / "out")
        assert refused.value.code == "type"
        assert clamd.received == []

    def test_malware_is_rejected_and_logged_by_signature_only(
        self, tmp_path: Path, clamd: Behaviour, caplog: pytest.LogCaptureFixture
    ) -> None:
        with (
            caplog.at_level("INFO", logger="gmat.security"),
            pytest.raises(processing.Rejected) as refused,
        ):
            processing.process(write(tmp_path, MALWARE_PDF), tmp_path / "out")
        assert (refused.value.code, refused.value.signature) == ("malware", SIGNATURE)
        (record,) = [r for r in caplog.records if r.name == "gmat.security"]
        assert record.levelname == "WARNING"
        assert record.security["signature"] == SIGNATURE  # type: ignore[attr-defined]

    def test_a_scanner_down_stores_nothing(self, tmp_path: Path, settings: Any) -> None:
        settings.ATTACHMENT_SCAN_SOCKET = refused_socket(tmp_path)
        with pytest.raises(processing.Rejected) as refused:
            processing.process(write(tmp_path), tmp_path / "out")
        assert refused.value.code == "unavailable"
        assert not (tmp_path / "out").exists()


@pytest.mark.django_db
class TestUpload:
    def test_malware_is_refused_audited_and_its_slot_given_back(
        self, people: People, draft: MaterialRequest, attach: Attach, clamd: Behaviour
    ) -> None:
        client = signed_in(people.tech_mad, HTTP_ACCEPT_LANGUAGE="en")
        response = send(client, draft, MALWARE_PDF, name="nota.pdf")
        assert response.status_code == 422
        assert b"virus scanner" in response.content
        assert not Attachment.objects.exists()
        assert list(AttachmentReservation.objects.values_list("state", flat=True)) == ["RELEASED"]
        event = AuditEvent.objects.get(action="attachment.malware_refused")
        assert event.data["signature"] == SIGNATURE
        assert event.object_id == draft.pk
        assert "nota" not in str(event.data)

    def test_a_refusal_stands_if_it_cannot_be_audited(
        self,
        people: People,
        draft: MaterialRequest,
        clamd: Behaviour,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        def broken(*args: Any, **kwargs: Any) -> None:
            raise RuntimeError("the audit is down")

        monkeypatch.setattr("apps.attachments.services._record", broken)
        client = signed_in(people.tech_mad, HTTP_ACCEPT_LANGUAGE="en")
        assert send(client, draft, MALWARE_PDF, name="nota.pdf").status_code == 422
        assert not Attachment.objects.exists()

    def test_a_clean_file_is_stored(
        self, people: People, draft: MaterialRequest, clamd: Behaviour
    ) -> None:
        client = signed_in(people.tech_mad, HTTP_ACCEPT_LANGUAGE="en")
        assert send(client, draft, PDF_BYTES, name="nota.pdf").status_code == 302
        assert Attachment.objects.filter(file_name="nota.pdf").exists()

    def test_a_scanner_down_answers_503_and_to_try_again(
        self, people: People, draft: MaterialRequest, settings: Any
    ) -> None:
        directory = Path(tempfile.mkdtemp(prefix="gone-"))
        settings.ATTACHMENT_SCAN_SOCKET = refused_socket(directory)
        client = signed_in(people.tech_mad, HTTP_ACCEPT_LANGUAGE="en")
        response = send(client, draft, PDF_BYTES, name="nota.pdf")
        assert response.status_code == 503
        assert response["Retry-After"] == "30"
        assert not Attachment.objects.exists()
        assert list(AttachmentReservation.objects.values_list("state", flat=True)) == ["RELEASED"]

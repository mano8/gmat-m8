"""The attachment screens (plan §5, F73): the upload entry point refuses before reading the
body, admits while the body is parsed, releases on every failure; the download is
authorized, sandboxed and never inline; removal follows the parent's rules.

Who gets which status on each URL is in the acceptance matrix; here, the behaviour.
"""

import sys
import time
from typing import Any
from uuid import UUID

import pytest
from django.conf import settings
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client
from django.urls import reverse
from django.utils import timezone

from apps.access.principals import SessionPrincipal
from apps.attachments import files, processing, services
from apps.attachments.models import Attachment, AttachmentReservation
from apps.attachments.services import ADD_OPERATION
from apps.attachments.uploads import ReservingUploadHandler, UploadAborted
from apps.core.idempotency import issue_key
from apps.core.locking import RetryExhausted
from apps.handovers.models import Handover
from apps.material_requests.models import MaterialRequest
from apps.receiving.models import GoodsReceipt
from tests.org import People
from tests.org import client_for as signed_in
from tests.unit.attachments.conftest import PDF_BYTES, Attach, admit, png, submit

pytestmark = pytest.mark.django_db

State = AttachmentReservation.State
ORIGIN = "http://testserver"


def upload_url(parent: Any, back: str = "") -> str:
    kind = {MaterialRequest: "request", GoodsReceipt: "receipt", Handover: "handover"}
    url = reverse(f"attachments:upload_{kind[type(parent)]}", args=[parent.pk])
    return f"{url}?back={back}" if back else url


def send(
    client: Client,
    parent: Any,
    data: bytes | None = None,
    *,
    name: str = "photo.png",
    origin: str | None = ORIGIN,
    fields: dict[str, Any] | None = None,
    meta: dict[str, str] | None = None,
    **extra: Any,
) -> Any:
    body: dict[str, Any] = {"file": SimpleUploadedFile(name, png() if data is None else data)}
    body.update(fields or {})
    headers = {"Origin": origin} if origin else {}
    url = upload_url(parent, extra.pop("back", ""))
    environ: dict[str, Any] = {**extra, **(meta or {})}  # WSGI keys (CONTENT_LENGTH, …)
    return client.post(url, body, headers=headers, **environ)


def states() -> list[str]:
    return list(AttachmentReservation.objects.values_list("state", flat=True))


def client_for(user: Any, **kwargs: Any) -> Client:
    """Signed in, in English (the messages below are compared in English)."""
    return signed_in(user, HTTP_ACCEPT_LANGUAGE="en", **kwargs)


def messages_of(response: Any) -> list[str]:
    return [str(message) for message in response.context["messages"]]


@pytest.fixture
def technician(people: People) -> Client:
    return client_for(people.tech_mad, mfa=False)


@pytest.fixture
def operator(people: People) -> Client:
    return client_for(people.wo_mad)


class TestUpload:
    def test_the_requester_attaches_a_photo_to_their_draft(
        self, technician: Client, draft: MaterialRequest, people: People
    ) -> None:
        key = issue_key(people.tech_mad.pk, ADD_OPERATION)
        response = send(technician, draft, fields={"idempotency_key": key}, name="obra.png")
        assert response.status_code == 302
        assert response["Location"] == reverse("requests:detail", args=[draft.pk])
        item = Attachment.objects.get()
        assert (item.request, item.file_name) == (draft, "obra.webp")
        assert files.stored_path(item.storage_key).exists()
        assert states() == [State.FINALIZED]
        page = technician.get(response["Location"])
        assert "obra.webp attached." in messages_of(page)

    def test_a_double_tap_stores_one_file(
        self, technician: Client, draft: MaterialRequest, people: People
    ) -> None:
        key = issue_key(people.tech_mad.pk, ADD_OPERATION)
        for _ in range(2):
            assert send(technician, draft, fields={"idempotency_key": key}).status_code == 302
        assert Attachment.objects.count() == 1
        assert sorted(states()) == [State.FINALIZED, State.RELEASED]

    def test_a_warehouse_operator_attaches_a_delivery_note_to_a_receipt(
        self, operator: Client, receipt: GoodsReceipt
    ) -> None:
        response = send(operator, receipt, PDF_BYTES, name="albaran.pdf")
        assert response["Location"] == reverse("receiving:detail", args=[receipt.pk])
        assert Attachment.objects.get().receipt == receipt

    def test_back_leads_to_the_named_page_of_the_parent_only(
        self, technician: Client, draft: MaterialRequest
    ) -> None:
        response = send(technician, draft, back="examine")
        assert response["Location"] == reverse("requests:examine", args=[draft.pk])
        response = send(technician, draft, back="https://evil.example/")
        assert response["Location"] == reverse("requests:detail", args=[draft.pk])

    @pytest.mark.parametrize("origin", [None, "https://evil.example", "null"])
    def test_a_cross_site_upload_is_refused_before_admission(
        self, technician: Client, draft: MaterialRequest, origin: str | None
    ) -> None:
        assert send(technician, draft, origin=origin).status_code == 403
        assert states() == []

    def test_a_signed_out_upload_is_refused_before_admission(self, draft: MaterialRequest) -> None:
        response = send(Client(), draft)
        assert (response.status_code, response["Location"].split("?")[0]) == (
            302,
            reverse("account_login"),
        )
        assert states() == []

    @pytest.mark.parametrize(
        ("extra", "status"),
        [
            ({"HTTP_TRANSFER_ENCODING": "chunked"}, 411),
            ({"CONTENT_LENGTH": "not a number"}, 411),
            ({"CONTENT_LENGTH": str(10 * 1024 * 1024 + 64 * 1024 + 1)}, 413),
        ],
    )
    def test_the_declared_length_is_checked_before_the_body(
        self, technician: Client, draft: MaterialRequest, extra: dict[str, str], status: int
    ) -> None:
        assert send(technician, draft, meta=extra).status_code == status
        assert states() == []

    def test_only_a_multipart_body_is_admitted(
        self, technician: Client, draft: MaterialRequest
    ) -> None:
        response = technician.post(
            upload_url(draft), b"{}", content_type="application/json", headers={"Origin": ORIGIN}
        )
        assert response.status_code == 400
        assert states() == []

    def test_a_third_concurrent_upload_gets_429(
        self, technician: Client, draft: MaterialRequest, tech: SessionPrincipal
    ) -> None:
        admit(tech, draft, 100)
        admit(tech, draft, 100)
        response = send(technician, draft)
        assert response.status_code == 429
        assert response["Retry-After"] == "30"
        assert states() == [State.PROCESSING, State.PROCESSING]

    def test_a_full_document_and_a_full_storage(
        self, settings: Any, technician: Client, draft: MaterialRequest, attach: Attach
    ) -> None:
        settings.ATTACHMENT_MAX_PER_PARENT = 1
        assert send(technician, draft).status_code == 302
        assert send(technician, draft).status_code == 409
        settings.ATTACHMENT_MAX_PER_PARENT = 10
        settings.ATTACHMENT_STORAGE_LIMIT = 200  # below the declared length of the next one
        response = send(technician, draft)
        assert response.status_code == 507
        assert "storage is full" in response.content.decode()

    def test_a_disguised_file_is_refused_and_released(
        self, technician: Client, draft: MaterialRequest
    ) -> None:
        svg = b"<svg xmlns='http://www.w3.org/2000/svg'><script>alert(1)</script></svg>"
        response = send(technician, draft, svg, name="photo.jpg")
        assert response.status_code == 415
        assert states() == [State.RELEASED]
        assert not Attachment.objects.exists()
        assert list(files.staging_dir().iterdir()) == []

    def test_a_hung_decoder_ends_before_a_shortened_lease(
        self,
        settings: Any,
        technician: Client,
        draft: MaterialRequest,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        settings.ATTACHMENT_LEASE = 6
        settings.ATTACHMENT_DECODE_TIMEOUT = 1
        hang = [sys.executable, "-c", "import time; time.sleep(60)"]
        monkeypatch.setattr(processing, "decoder_command", lambda *args: hang)
        started = time.monotonic()
        response = send(technician, draft)
        assert response.status_code == 422
        assert time.monotonic() - started < settings.ATTACHMENT_LEASE
        reservation = AttachmentReservation.objects.get()
        assert reservation.state == State.RELEASED
        assert reservation.finished_at is not None
        assert reservation.finished_at < reservation.lease_until

    def test_a_body_without_its_file_is_released(
        self, technician: Client, draft: MaterialRequest
    ) -> None:
        response = technician.post(upload_url(draft), {"note": "x"}, headers={"Origin": ORIGIN})
        assert response.status_code == 400
        assert states() == [State.RELEASED]

    def test_a_second_file_aborts_the_upload(
        self, technician: Client, draft: MaterialRequest
    ) -> None:
        body = {
            "file": [SimpleUploadedFile("a.png", png()), SimpleUploadedFile("b.png", png())],
        }
        response = technician.post(upload_url(draft), body, headers={"Origin": ORIGIN})
        assert response.status_code == 400
        assert states() == [State.RELEASED]
        assert list(files.staging_dir().iterdir()) == []

    def test_a_csrf_failure_releases_its_reservation(
        self, people: People, draft: MaterialRequest
    ) -> None:
        client = client_for(people.tech_mad, mfa=False, enforce_csrf_checks=True)
        client.cookies[settings.CSRF_COOKIE_NAME] = "a" * 32
        response = send(client, draft, fields={"csrfmiddlewaretoken": "b" * 32})
        assert response.status_code == 403
        assert states() == [State.RELEASED]  # admitted while CSRF parsed the body, then freed

    def test_a_valid_csrf_token_is_accepted(self, people: People, draft: MaterialRequest) -> None:
        client = client_for(people.tech_mad, mfa=False, enforce_csrf_checks=True)
        client.cookies[settings.CSRF_COOKIE_NAME] = "a" * 32
        response = send(client, draft, fields={"csrfmiddlewaretoken": "a" * 32})
        assert response.status_code == 302
        assert states() == [State.FINALIZED]

    def test_a_document_left_out_of_reach_is_not_found(
        self, people: People, receipt: GoodsReceipt
    ) -> None:
        assert send(client_for(people.mm_sev), receipt).status_code == 404
        # A warehouse receipt: a technician receives at worksites only (Q44), so it is
        # out of their reach, not merely beyond their rights.
        assert send(client_for(people.tech_mad, mfa=False), receipt).status_code == 404
        assert send(client_for(people.nobody, mfa=False), receipt).status_code == 403
        assert states() == []

    def test_an_unknown_kind_is_not_found(self, technician: Client) -> None:
        url = reverse("attachments:upload_request", args=[UUID(int=7)]).replace("request", "x")
        assert technician.post(url, headers={"Origin": ORIGIN}).status_code == 404


class TestHandler:
    def test_a_stream_that_exceeds_its_reservation_is_aborted(
        self, rf: Any, draft: MaterialRequest, tech: SessionPrincipal
    ) -> None:
        reservation = admit(tech, draft, 10)
        handler = ReservingUploadHandler(rf.post("/"), admit=lambda declared: reservation)
        handler.handle_raw_input(None, {}, 10, b"x")
        handler.new_file("file", "a.png", "image/png", 10)
        handler.receive_data_chunk(b"0123456789", 0)
        with pytest.raises(UploadAborted) as caught:
            handler.receive_data_chunk(b"!", 10)
        assert caught.value.code == "too_large"
        handler.upload_interrupted()
        assert files.staged_input(reservation.pk).read_bytes() == b"0123456789"

    def test_data_without_an_accepted_part_is_refused(
        self, rf: Any, draft: MaterialRequest, tech: SessionPrincipal
    ) -> None:
        reservation = admit(tech, draft, 10)
        handler = ReservingUploadHandler(rf.post("/"), admit=lambda declared: reservation)
        with pytest.raises(UploadAborted):
            handler.new_file("other", "a.png", "image/png", 10)  # not admitted yet
        handler.handle_raw_input(None, {}, 10, b"x")
        with pytest.raises(UploadAborted):
            handler.new_file("other", "a.png", "image/png", 10)
        with pytest.raises(UploadAborted):
            handler.receive_data_chunk(b"x", 0)


class TestDownload:
    def test_the_file_is_offered_as_a_sandboxed_attachment(
        self, technician: Client, draft: MaterialRequest, tech: SessionPrincipal, attach: Attach
    ) -> None:
        item = attach(tech, draft, PDF_BYTES, name="nota.pdf")
        response = technician.get(reverse("attachments:download", args=[item.pk]))
        assert response.status_code == 200
        assert response.getvalue() == PDF_BYTES
        assert response["Content-Type"] == "application/pdf"
        assert response["Content-Disposition"] == 'attachment; filename="nota.pdf"'
        assert response["X-Content-Type-Options"] == "nosniff"
        assert response["Content-Security-Policy"] == "sandbox; default-src 'none'"
        assert "no-store" in response["Cache-Control"]

    def test_only_who_sees_the_document_downloads(
        self, people: People, draft: MaterialRequest, tech: SessionPrincipal, attach: Attach
    ) -> None:
        url = reverse("attachments:download", args=[attach(tech, draft).pk])
        assert client_for(people.mm_sev).get(url).status_code == 404
        assert client_for(people.nobody, mfa=False).get(url).status_code == 403
        assert Client().get(url).status_code == 302

    def test_a_removed_or_missing_file_is_gone(
        self, technician: Client, draft: MaterialRequest, tech: SessionPrincipal, attach: Attach
    ) -> None:
        missing = attach(tech, draft)
        files.stored_path(missing.storage_key).unlink()
        assert technician.get(reverse("attachments:download", args=[missing.pk])).status_code == 404
        removed = attach(tech, draft)
        technician.post(reverse("attachments:remove", args=[removed.pk]))
        assert technician.get(reverse("attachments:download", args=[removed.pk])).status_code == 404


class TestRemove:
    def test_the_uploader_removes_from_a_draft(
        self, technician: Client, draft: MaterialRequest, tech: SessionPrincipal, attach: Attach
    ) -> None:
        item = attach(tech, draft)
        response = technician.post(reverse("attachments:remove", args=[item.pk]))
        assert response["Location"] == reverse("requests:detail", args=[draft.pk])
        item.refresh_from_db()
        assert item.removed_at is not None

    def test_a_manager_removes_with_a_reason(
        self, people: People, draft: MaterialRequest, tech: SessionPrincipal, attach: Attach
    ) -> None:
        item = attach(tech, draft)
        submit(draft)
        manager = client_for(people.mm_mad)
        url = f"{reverse('attachments:remove', args=[item.pk])}?back=examine"
        response = manager.post(url, {"reason": ""})
        assert response["Location"] == reverse("requests:examine", args=[draft.pk])
        item.refresh_from_db()
        assert item.removed_at is None
        manager.post(url, {"reason": "Not ours"})
        item.refresh_from_db()
        assert (item.removed_by, item.removal_reason) == (people.mm_mad, "Not ours")
        assert manager.post(url, {"reason": "Again"}).status_code == 403

    def test_the_removal_is_a_dialog_that_asks_a_moderator_for_a_reason(
        self, people: People, draft: MaterialRequest, tech: SessionPrincipal, attach: Attach
    ) -> None:
        item = attach(tech, draft)
        submit(draft)
        manager = client_for(people.mm_mad)
        page = manager.get(reverse("requests:examine", args=[draft.pk])).content.decode()
        assert (
            f'id="remove-dialog-{item.pk}"' in page
            and "<details" not in page.split("<main", 1)[1].split("</main>", 1)[0]
        )
        assert f'id="id_remove_{item.pk}_reason"' in page
        url = f"{reverse('attachments:remove', args=[item.pk])}?back=examine"
        again = manager.post(url, {"reason": ""}, headers={"HX-Request": "true"})
        assert again.status_code == 422
        assert 'data-testid="attachment-remove-form"' in again.content.decode()
        done = manager.post(url, {"reason": "Not ours"}, headers={"HX-Request": "true"})
        assert done.status_code == 204
        assert done["HX-Redirect"] == reverse("requests:examine", args=[draft.pk])

    def test_the_uploader_has_no_reason_to_give_on_a_draft(
        self, technician: Client, draft: MaterialRequest, tech: SessionPrincipal, attach: Attach
    ) -> None:
        item = attach(tech, draft)
        page = technician.get(reverse("requests:detail", args=[draft.pk])).content.decode()
        assert f'id="remove-dialog-{item.pk}"' in page
        assert f'id="id_remove_{item.pk}_reason"' not in page

    def test_the_technician_cannot_remove_after_submission(
        self, technician: Client, draft: MaterialRequest, tech: SessionPrincipal, attach: Attach
    ) -> None:
        item = attach(tech, draft)
        submit(draft)
        assert technician.post(reverse("attachments:remove", args=[item.pk])).status_code == 403

    def test_a_signed_out_removal_goes_to_the_login(
        self, draft: MaterialRequest, tech: SessionPrincipal, attach: Attach
    ) -> None:
        response = Client().post(reverse("attachments:remove", args=[attach(tech, draft).pk]))
        assert response.status_code == 302


class TestPanels:
    def test_the_draft_offers_the_upload_to_its_requester(
        self, technician: Client, draft: MaterialRequest, tech: SessionPrincipal, attach: Attach
    ) -> None:
        item = attach(tech, draft)
        page = technician.get(reverse("requests:detail", args=[draft.pk])).content.decode()
        assert 'data-testid="attachment-upload"' in page
        assert reverse("attachments:download", args=[item.pk]) in page
        assert 'data-testid="attachment-remove-open"' in page
        # The uploader's own draft: removing a file asks for no reason.
        assert f'id="id_remove_{item.pk}_reason"' not in page

    def test_the_review_page_lists_and_moderates(
        self, people: People, draft: MaterialRequest, tech: SessionPrincipal, attach: Attach
    ) -> None:
        item = attach(tech, draft)
        submit(draft)
        page = client_for(people.mm_mad).get(reverse("requests:examine", args=[draft.pk]))
        html = page.content.decode()
        assert item.file_name in html
        assert 'data-testid="attachment-upload"' not in html
        assert "?back=examine" in html
        assert 'name="reason"' in html
        assert "File attached" in html  # in the request's history

    def test_a_receipt_and_a_handover_show_their_files(
        self,
        operator: Client,
        receipt: GoodsReceipt,
        handover: Handover,
        attach: Attach,
        mm: SessionPrincipal,
        people: People,
    ) -> None:
        attach(mm, handover, PDF_BYTES, name="entrega.pdf")
        page = operator.get(reverse("receiving:detail", args=[receipt.pk])).content.decode()
        assert 'data-testid="attachment-upload"' in page
        page = operator.get(reverse("handovers:detail", args=[handover.pk])).content.decode()
        assert "entrega.pdf" in page
        assert 'data-testid="attachment-remove-open"' not in page  # removal is the managers'

    def test_a_removed_file_is_listed_with_who_removed_it(
        self, people: People, receipt: GoodsReceipt, mm: SessionPrincipal, attach: Attach
    ) -> None:
        item = attach(mm, receipt)
        client = client_for(people.mm_mad)
        client.post(reverse("attachments:remove", args=[item.pk]), {"reason": "Blurred"})
        page = client.get(reverse("receiving:detail", args=[receipt.pk])).content.decode()
        assert 'data-testid="attachment-removed"' in page
        assert "Blurred" in page
        assert reverse("attachments:download", args=[item.pk]) not in page

    def test_a_voided_receipt_takes_no_more_files(
        self, operator: Client, receipt: GoodsReceipt
    ) -> None:
        GoodsReceipt.objects.filter(pk=receipt.pk).update(
            status=GoodsReceipt.Status.VOIDED,
            voided_at=timezone.now(),
            voided_by=receipt.received_by,
            void_reason="wrong",
        )
        page = operator.get(reverse("receiving:detail", args=[receipt.pk])).content.decode()
        assert 'data-testid="attachments"' not in page
        assert send(operator, receipt).status_code == 409


class TestUploadFailures:
    def test_a_tampered_key_is_refused_and_released(
        self, technician: Client, draft: MaterialRequest
    ) -> None:
        assert send(technician, draft, fields={"idempotency_key": "tampered"}).status_code == 400
        assert states() == [State.RELEASED]

    def test_a_conflict_that_keeps_conflicting_asks_to_retry(
        self, technician: Client, draft: MaterialRequest, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def conflicting(*args: Any, **kwargs: Any) -> Any:
            raise RetryExhausted

        monkeypatch.setattr(services, "admit_upload", conflicting)
        assert send(technician, draft).status_code == 409

    def test_a_release_that_fails_keeps_the_original_answer(
        self, technician: Client, draft: MaterialRequest, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def broken(*args: Any, **kwargs: Any) -> Any:
            raise RuntimeError("database gone")

        monkeypatch.setattr(services, "release_reservation", broken)
        response = technician.post(upload_url(draft), {"note": "x"}, headers={"Origin": ORIGIN})
        assert response.status_code == 400
        assert states() == [State.PROCESSING]  # left to the lease expiry

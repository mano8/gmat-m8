"""The upload handler that admits an upload before its body is read (plan §5, F73).

``views.upload`` installs ``ReservingUploadHandler`` as the request's only upload handler,
then lets ``csrf_protect`` parse the body through it:

- ``handle_raw_input`` runs **admission** (``services.admit_upload``) before the parser
  reads any byte of the body; a refusal propagates as the service's ``ValidationError``;
- the one file part (``FIELD``) streams to the reservation's staging file, and
  ``receive_data_chunk`` aborts the upload as soon as the bytes received exceed what was
  reserved (``UploadAborted``);
- any other file part aborts it too.

Django's own handlers never run, so nothing of the body is kept in memory or in another
temporary file. The view releases the reservation whatever happens after admission.
"""

from collections.abc import Callable
from typing import Any, BinaryIO
from uuid import UUID

from django.core.files.uploadhandler import FileUploadHandler
from django.http import HttpRequest

from apps.attachments import files
from apps.attachments.models import AttachmentReservation

FIELD = "file"


class UploadAborted(Exception):
    """The body broke the upload's rules; ``code`` names which (``views.STATUSES``)."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class ReservingUploadHandler(FileUploadHandler):
    """Admit, then stream exactly one file to the staging folder within its reservation."""

    def __init__(self, request: HttpRequest, admit: Callable[[int], AttachmentReservation]) -> None:
        super().__init__(request)
        self.admit = admit
        self.reservation_id: UUID | None = None
        self.reserved = 0
        self.received = 0
        self.sent_name = ""
        self.complete = False
        self._handle: BinaryIO | None = None

    def handle_raw_input(
        self,
        input_data: Any,
        META: dict[str, Any],
        content_length: int,
        boundary: Any,
        encoding: str | None = None,
    ) -> None:
        reservation = self.admit(content_length)
        self.reservation_id, self.reserved = reservation.pk, reservation.reserved_bytes

    def new_file(self, field_name: str, file_name: str, *args: Any, **kwargs: Any) -> None:
        super().new_file(field_name, file_name, *args, **kwargs)
        if self.reservation_id is None or field_name != FIELD or self._handle or self.complete:
            raise UploadAborted("malformed")
        files.ensure_dirs()
        self.sent_name = file_name or ""
        self._handle = files.staged_input(self.reservation_id).open("xb")

    def receive_data_chunk(self, raw_data: bytes, start: int) -> None:
        self.received += len(raw_data)
        if self.received > self.reserved:
            raise UploadAborted("too_large")
        if self._handle is None:  # data of a part that new_file refused
            raise UploadAborted("malformed")
        self._handle.write(raw_data)

    def file_complete(self, file_size: int) -> None:
        """The file stays in staging; ``views.upload`` reads ``complete``, not FILES."""
        self.close()
        self.complete = True

    def upload_interrupted(self) -> None:
        self.close()

    def close(self) -> None:
        if self._handle is not None:
            self._handle.close()
            self._handle = None

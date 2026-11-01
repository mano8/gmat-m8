"""Fixtures of the attachment tests: one parent of each type, files to upload and ``attach``,
which runs the three phases through the services as the upload view does.

Every test stores its files in its own ``ATTACHMENT_ROOT`` (``tmp_path``).
"""

import io
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from django.db.models import Model
from django.utils import timezone
from PIL import Image

from apps.access.principals import SessionPrincipal
from apps.attachments import files, services
from apps.attachments.models import Attachment, AttachmentReservation
from apps.attachments.parents import type_of
from apps.handovers.models import Handover
from apps.material_requests.models import MaterialRequest, MaterialRequestLine
from apps.receiving.models import GoodsReceipt
from tests.org import Org, People
from tests.support import principal_for
from tests.unit.handovers.conftest import hand_over  # noqa: F401 - fixtures used by these tests
from tests.unit.purchasing.conftest import (  # noqa: F401
    Buy,
    MakeLine,
    breaker,
    buy,
    cable,
    make_line,
    mm,
)
from tests.unit.receiving.conftest import Receive, receive, third  # noqa: F401

PDF_BYTES = b"%PDF-1.4\n1 0 obj << /Type /Catalog >> endobj\ntrailer << /Root 1 0 R >>\n%%EOF\n"


@pytest.fixture(autouse=True)
def media(settings: Any, tmp_path: Path) -> Path:
    settings.ATTACHMENT_ROOT = str(tmp_path / "media")
    return tmp_path / "media"


def png(width: int = 40, height: int = 30, mode: str = "RGB", **save: Any) -> bytes:
    buffer = io.BytesIO()
    Image.new(mode, (width, height), "red" if mode == "RGB" else 0).save(buffer, "PNG", **save)
    return buffer.getvalue()


def jpeg(width: int = 40, height: int = 30, **save: Any) -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", (width, height), "blue").save(buffer, "JPEG", **save)
    return buffer.getvalue()


@pytest.fixture
def draft(org: Org, people: People, cable: Any) -> MaterialRequest:  # noqa: F811
    """A draft request of the A-MAD technician for the hospital."""
    request = MaterialRequest.objects.create(
        project=org.hospital, procurement_branch=org.a_mad, requested_by=people.tech_mad
    )
    MaterialRequestLine.objects.create(
        request=request,
        project=org.hospital,
        line_no=1,
        material=cable,
        unit=cable.unit,
        requested_qty="5",
    )
    return request


@pytest.fixture
def tech(people: People) -> SessionPrincipal:
    return principal_for(people.tech_mad)


@pytest.fixture
def receipt(make_line: MakeLine, buy: Buy, receive: Receive) -> GoodsReceipt:  # noqa: F811
    """A receipt at A-MAD's warehouse, posted by A-MAD's material manager."""
    line = make_line("10")
    return receive(buy((line, "10")), "4")


@pytest.fixture
def handover(make_line: MakeLine, hand_over: Any) -> Handover:  # noqa: F811
    """A handover from A-MAD's warehouse to the hospital."""
    line = make_line("10", sourcing=MaterialRequestLine.Sourcing.STOCK)
    result: Handover = hand_over((line, "4"))
    return result


type Attach = Callable[..., Attachment]


def admit(principal: SessionPrincipal, parent: Model, size: int) -> AttachmentReservation:
    return services.admit_upload(principal, type_of(parent).kind, parent.pk, declared_bytes=size)


def stage(reservation: AttachmentReservation, data: bytes) -> None:
    files.ensure_dirs()
    files.staged_input(reservation.pk).write_bytes(data)


@pytest.fixture
def attach() -> Attach:
    """``attach(principal, parent, data, name=…, key=…)``: admit, stage, finalize."""

    def make(
        principal: SessionPrincipal,
        parent: Model,
        data: bytes | None = None,
        *,
        name: str = "photo.png",
        key: str | None = None,
        declared: int | None = None,
    ) -> Attachment:
        content = png() if data is None else data
        reservation = admit(principal, parent, declared or len(content) + 500)
        stage(reservation, content)
        return services.finalize_upload(
            principal, reservation.pk, file_name=name, idempotency_key=key
        )

    return make


def submit(request: MaterialRequest) -> None:
    """The request leaves its draft (written directly: the review flow is not under test)."""
    MaterialRequest.objects.filter(pk=request.pk).update(
        status=MaterialRequest.Status.SUBMITTED,
        number=f"SOL-A-MAD-2026-{MaterialRequest.objects.count():05d}",
        submitted_at=timezone.now(),
    )

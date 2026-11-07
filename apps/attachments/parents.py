"""The three parent types an attachment may belong to, and their rules (plan §5).

``attachment.*`` are not capabilities: every operation is checked against the parent.

| Parent | View | Add | Remove with a reason |
| --- | --- | --- | --- |
| Request | ``request.view`` | ``request.create``, the requester, ``DRAFT`` | ``request.source`` |
| Receipt | ``receipt.view`` | ``receipt.post``, ``POSTED`` | ``receipt.void`` |
| Handover | ``handover.view`` | ``handover.post``, ``POSTED`` | ``handover.void`` |

The "remove with a reason" capabilities are those only group administrators, material
managers and engineers hold (§3.4, ADR-0014), each on its own parent. Besides them, the
uploader may remove their own file while the parent is still a draft (only requests have
drafts); every removal is a soft delete, and the file counts against the quota until the
purge.
"""

from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, cast
from uuid import UUID

from django.db.models import Model
from django.urls import reverse

from apps.access.roles import Capability
from apps.core.locking import LockLevel
from apps.handovers.models import Handover
from apps.material_requests.models import MaterialRequest
from apps.receiving.models import GoodsReceipt


class Kind(StrEnum):
    REQUEST = "request"
    RECEIPT = "receipt"
    HANDOVER = "handover"


def _request_open(parent: Any) -> bool:
    return bool(parent.status == MaterialRequest.Status.DRAFT)


def _posted(parent: Any) -> bool:
    return bool(parent.status == "POSTED")


def _request_owner(parent: Any, user_id: UUID) -> bool:
    return bool(parent.requested_by_id == user_id)


def _anyone(parent: Any, user_id: UUID) -> bool:
    return True


@dataclass(frozen=True)
class ParentType:
    """What an attachment operation checks and locks for one kind of parent.

    ``field``: the foreign key on ``Attachment`` and ``AttachmentReservation``. ``level``:
    where the parent header is locked (§4.7). ``is_open``: the parent takes new files.
    ``is_draft``: the uploader may still remove their own. ``may_add``: who may add beyond
    the capability (a request's requester). ``pages``: the parent's pages a form may go back
    to, the first being the default. ``verified``: the fields an authorization read that can
    still change (a draft request's); they are compared again under the parent's lock.
    """

    kind: Kind
    model: type[Model]
    field: str
    level: LockLevel
    view: str
    add: str
    moderate: str
    is_open: Callable[[Any], bool]
    is_draft: Callable[[Any], bool]
    may_add: Callable[[Any, UUID], bool]
    pages: tuple[tuple[str, str], ...]
    verified: tuple[str, ...] = ()

    def page(self, parent: Model, back: str = "") -> str:
        """The URL of the parent page named ``back`` (the default page if unknown)."""
        names = dict(self.pages)
        name = names.get(back, self.pages[0][1])
        return reverse(name, args=[parent.pk])


PARENTS: dict[Kind, ParentType] = {
    Kind.REQUEST: ParentType(
        kind=Kind.REQUEST,
        model=MaterialRequest,
        field="request",
        level=LockLevel.REQUEST_HEADERS,
        view=Capability.REQUEST_VIEW,
        add=Capability.REQUEST_CREATE,
        moderate=Capability.REQUEST_SOURCE,
        is_open=_request_open,
        is_draft=_request_open,
        may_add=_request_owner,
        pages=(("detail", "requests:detail"), ("examine", "requests:examine")),
        verified=("project_id", "procurement_branch_id", "requested_by_id", "status"),
    ),
    Kind.RECEIPT: ParentType(
        kind=Kind.RECEIPT,
        model=GoodsReceipt,
        field="receipt",
        level=LockLevel.RECEIPTS,
        view=Capability.RECEIPT_VIEW,
        add=Capability.RECEIPT_POST,
        moderate=Capability.RECEIPT_VOID,
        is_open=_posted,
        is_draft=lambda parent: False,
        may_add=_anyone,
        pages=(("detail", "receiving:detail"),),
    ),
    Kind.HANDOVER: ParentType(
        kind=Kind.HANDOVER,
        model=Handover,
        field="handover",
        level=LockLevel.HANDOVERS,
        view=Capability.HANDOVER_VIEW,
        add=Capability.HANDOVER_POST,
        moderate=Capability.HANDOVER_VOID,
        is_open=_posted,
        is_draft=lambda parent: False,
        may_add=_anyone,
        pages=(("detail", "handovers:detail"),),
    ),
}


def parent_type(kind: str) -> ParentType:
    """The parent type for a URL's ``kind`` (``ValueError`` for an unknown one)."""
    return PARENTS[Kind(kind)]


def type_of(parent: Model) -> ParentType:
    for candidate in PARENTS.values():
        if isinstance(parent, candidate.model):
            return candidate
    raise TypeError(f"{type(parent).__name__} takes no attachments")


def parent_of(row: Any) -> Model:
    """The parent of an ``Attachment`` or ``AttachmentReservation``."""
    for candidate in PARENTS.values():
        if getattr(row, f"{candidate.field}_id") is not None:
            return cast(Model, getattr(row, candidate.field))
    raise ValueError("The row has no parent")

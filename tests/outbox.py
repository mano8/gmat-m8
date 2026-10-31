"""Outbox test helpers shared by the unit and integration layers."""

from dataclasses import dataclass, field
from uuid import UUID

from apps.core.locking import LockLevel, LockMode, LockOrder
from apps.notifications.models import EmailOutbox, Status
from apps.purchasing.models import PurchaseOrder

SECRET_BODY = "Reset link: https://app.example.org/accounts/password/reset/key/1-SECRETTOKEN/"


@dataclass
class FakeOrders:
    """A ``DispatchSource`` over real purchase orders (the purchasing app registers its own
    in M5.3).

    It takes the order lock like the purchasing app will (``FOR SHARE`` at L4), refuses
    per order, and records every ``after_outcome`` call.
    """

    refusals: dict[UUID, Status] = field(default_factory=dict)
    locked: list[UUID] = field(default_factory=list)
    outcomes: list[tuple[UUID, str]] = field(default_factory=list)

    def lock_order(self, locks: LockOrder, po_id: UUID) -> None:
        self.locked.append(po_id)
        locks.lock(LockLevel.PO_HEADERS, PurchaseOrder, [po_id], mode=LockMode.SHARE)

    def refusal(self, message: EmailOutbox) -> Status | None:
        return self.refusals.get(message.source_po_id)  # type: ignore[arg-type]

    def after_outcome(self, message: EmailOutbox, locks: LockOrder) -> None:
        assert locks.level == LockLevel.PO_OUTBOX
        self.outcomes.append((message.pk, message.status))

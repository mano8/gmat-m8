"""Outbox test helpers shared by the unit and integration layers."""

import os
import subprocess
import sys
import textwrap
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from uuid import UUID

from django.db import connection

from apps.access.principals import SystemPrincipal
from apps.core.locking import LockLevel, LockMode, LockOrder
from apps.notifications import dispatch
from apps.notifications.models import EmailAttempt, EmailOutbox, Kind, Status
from apps.purchasing.models import PurchaseOrder

ROOT = Path(__file__).resolve().parents[1]
SECRET_BODY = "Reset link: https://app.example.org/accounts/password/reset/key/1-SECRETTOKEN/"


@dataclass
class FakeOrders:
    """A ``DispatchSource`` over real purchase orders, in place of the purchasing app's.

    It takes the order lock like the purchasing app (``FOR SHARE`` at L4), refuses per
    order, and records every ``after_outcome`` and ``record_attempt`` call.
    """

    refusals: dict[UUID, Status] = field(default_factory=dict)
    locked: list[UUID] = field(default_factory=list)
    outcomes: list[tuple[UUID, str]] = field(default_factory=list)
    attempts: list[tuple[UUID, int, str]] = field(default_factory=list)

    def lock_order(self, locks: LockOrder, po_id: UUID) -> None:
        self.locked.append(po_id)
        locks.lock(LockLevel.PO_HEADERS, PurchaseOrder, [po_id], mode=LockMode.SHARE)

    def refusal(self, message: EmailOutbox) -> Status | None:
        return self.refusals.get(message.source_po_id)  # type: ignore[arg-type]

    def after_outcome(self, message: EmailOutbox, locks: LockOrder) -> None:
        assert locks.level == LockLevel.PO_OUTBOX
        self.outcomes.append((message.pk, message.status))

    def record_attempt(
        self, principal: SystemPrincipal, message: EmailOutbox, attempt: EmailAttempt
    ) -> None:
        self.attempts.append((message.pk, attempt.number, attempt.outcome))


@contextmanager
def no_dispatch_source() -> Iterator[None]:
    """Take the purchasing app's source away for a test of the bare outbox (M2.5)."""
    saved = list(dispatch._source)
    dispatch._source.clear()
    try:
        yield
    finally:
        dispatch._source[:] = saved


def make_dispatch(
    order: PurchaseOrder,
    status: Status = Status.QUEUED,
    *,
    kind: Kind = Kind.PO_ORDER,
    revision: int | None = None,
    address: str = "orders@supplier.example",
) -> EmailOutbox:
    """A dispatch row of ``order`` in ``status``, as the purchasing app queues it (no attempt)."""
    revision = order.revision if revision is None else revision
    reference = (
        f"PO:{order.pk}:cancel" if kind == Kind.PO_CANCELLATION else f"PO:{order.pk}:rev{revision}"
    )
    return EmailOutbox.objects.create(
        kind=kind,
        status=status,
        recipient_address=address,
        source_po=order,
        source_ref=reference,
        from_email="gmat-m8 <no-reply@localhost>",
        subject=f"Order {order.number} rev {revision}",
        body="Please deliver.",
        attempts=0 if status == Status.QUEUED else 1,
    )


# A real worker process (``manage.py worker``'s ``main``) sending to the fake SMTP server on
# the port given as its argument; the integration tests kill it mid-send.
WORKER_SCRIPT = textwrap.dedent(
    """
    import os, sys
    import django

    django.setup()
    from django.conf import settings

    settings.MAILERS = {
        "default": {"BACKEND": "apps.notifications.backends.OutboxEmailBackend"},
        "outbound": {
            "BACKEND": "django.core.mail.backends.smtp.EmailBackend",
            "OPTIONS": {"host": "127.0.0.1", "port": int(sys.argv[1]), "timeout": 10},
        },
    }
    settings.OUTBOUND_EMAIL = True
    from apps.notifications.worker import main

    main()
    """
)


def start_worker(port: int, flags: Path) -> subprocess.Popen[bytes]:
    """Start ``WORKER_SCRIPT`` against the test database (a ``transaction=True`` test)."""
    env = {
        **os.environ,
        "DJANGO_SETTINGS_MODULE": "config.settings.test",
        "DB_NAME": str(connection.settings_dict["NAME"]),
        "WORKER_FLAGS_DIR": str(flags),
        "WORKER_HEARTBEAT_FILE": str(flags / "heartbeat"),
        "PYTHONPATH": str(ROOT),
    }
    return subprocess.Popen(  # noqa: S603 (fixed interpreter and script)
        [sys.executable, "-c", WORKER_SCRIPT, str(port)],
        cwd=ROOT,
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )

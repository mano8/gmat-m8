"""Derived quantities of request lines and the predicates built on them (plan §4.5, §4.9).

Nothing here is stored. A line's approved quantity A is its own; the other inputs come from
the documents that refer to it: received R, pending P (the waterfall) and draft
reservations Q from purchase orders and receipts (M5.1, M6.1), delivered D from handovers
(M6.2), and the allocated ceiling (Σ allocation ``qty`` on draft or issued orders) that a
cancellation must find at zero. Those apps refer to request lines, so this one cannot
import them: each registers a ``FlowSource`` from its ``AppConfig.ready``. Until they
exist every flow is zero.

Predicates (§4.5): a line is **live** when approved with ``approved_qty > 0`` and not
closed short, whatever the request's state; it is **open** when it is live and its request
is ``APPROVED``. The ledger quantities follow the plan's table; the commitment floor
max(D, R) + P + Q is state-independent, so an approved quantity can never be reduced below
what has been delivered, received or committed (I13).
"""

from collections.abc import Callable, Collection, Iterable, Mapping
from dataclasses import astuple, dataclass
from decimal import Decimal
from uuid import UUID

from django.db.models import Q

from apps.material_requests.models import MaterialRequest, MaterialRequestLine
from apps.organizations.deactivation import OpenDocument

ZERO = Decimal("0")

Status = MaterialRequest.Status
Decision = MaterialRequestLine.Decision


@dataclass(frozen=True)
class Flows:
    """What other documents contribute to one request line."""

    received: Decimal = ZERO  # R: attributed on posted receipts, over every order
    pending: Decimal = ZERO  # P: the waterfall over its open allocations
    reserved: Decimal = ZERO  # Q: allocations on draft orders
    delivered: Decimal = ZERO  # D: posted handovers of both kinds
    allocated: Decimal = ZERO  # Σ allocation qty on draft or issued orders

    def __add__(self, other: Flows) -> Flows:
        return Flows(*(a + b for a, b in zip(astuple(self), astuple(other), strict=True)))


type FlowSource = Callable[[Collection[UUID]], Mapping[UUID, Flows]]

_sources: list[FlowSource] = []


def register_flow_source(source: FlowSource) -> Callable[[], None]:
    """Register ``source`` (from an ``AppConfig.ready``); return its unregister function."""
    if source not in _sources:
        _sources.append(source)

    def unregister() -> None:
        if source in _sources:
            _sources.remove(source)

    return unregister


def flows(line_ids: Collection[UUID]) -> dict[UUID, Flows]:
    """The flows of these lines, summed over every registered source."""
    result = {line_id: Flows() for line_id in line_ids}
    for source in _sources:
        for line_id, part in source(list(result)).items():
            result[line_id] = result[line_id] + part
    return result


# --- predicates -----------------------------------------------------------------------


def live_q(prefix: str = "") -> Q:
    """Live lines, through ``prefix`` (``"request_line__"`` from an allocation)."""
    return Q(
        **{
            f"{prefix}decision": Decision.APPROVED,
            f"{prefix}approved_qty__gt": 0,
            f"{prefix}closed_short_at__isnull": True,
        }
    )


def open_q(prefix: str = "") -> Q:
    """Open lines: live, and their request is ``APPROVED``."""
    return live_q(prefix) & Q(**{f"{prefix}request__status": Status.APPROVED})


def is_live(line: MaterialRequestLine) -> bool:
    """Approved, with a positive approved quantity, and not closed short (state-independent)."""
    return (
        line.decision == Decision.APPROVED
        and line.approved_qty is not None
        and line.approved_qty > 0
        and line.closed_short_at is None
    )


@dataclass(frozen=True)
class LineLedger:
    """The ledger of one request line (plan §4.5, "Ledger")."""

    line: MaterialRequestLine
    flows: Flows
    is_open: bool

    @property
    def approved(self) -> Decimal:
        """A (zero until a line is approved)."""
        return self.line.approved_qty or ZERO

    @property
    def floor(self) -> Decimal:
        """max(D, R) + P + Q: the least the approved quantity may be (§4.4, F70)."""
        f = self.flows
        return max(f.delivered, f.received) + f.pending + f.reserved

    @property
    def committed(self) -> Decimal:
        return self.floor if self.is_open else ZERO

    @property
    def awaiting_handover(self) -> Decimal:
        if not self.is_open:
            return ZERO
        return max(ZERO, min(self.flows.received, self.approved) - self.flows.delivered)

    @property
    def to_purchase(self) -> Decimal:
        if not self.is_open or self.line.sourcing != MaterialRequestLine.Sourcing.PURCHASE:
            return ZERO
        return max(ZERO, self.approved - self.committed)

    @property
    def outstanding(self) -> Decimal:
        return self.approved - self.flows.delivered if self.is_open else ZERO

    @property
    def fully_delivered(self) -> bool:
        """D = A (I5 keeps D ≤ A)."""
        return self.flows.delivered >= self.approved


def line_ledgers(lines: Iterable[MaterialRequestLine]) -> list[LineLedger]:
    """The ledger of each line, in the order given (``line.request`` must be loaded)."""
    listed = list(lines)
    found = flows([line.pk for line in listed])
    return [
        LineLedger(
            line=line,
            flows=found[line.pk],
            is_open=line.request.status == Status.APPROVED and is_live(line),
        )
        for line in listed
    ]


def purchasable(lines: Iterable[MaterialRequestLine]) -> list[LineLedger]:
    """The lines with something left to purchase (open, ``PURCHASE``, *to purchase* > 0).

    A closed-short line is not live, so it never appears here (F26).
    """
    return [ledger for ledger in line_ledgers(lines) if ledger.to_purchase > 0]


# --- transition eligibility (§4.9) ----------------------------------------------------


def auto_close_eligible(ledgers: Iterable[LineLedger]) -> bool:
    """``APPROVED → CLOSED`` (auto): every live line has D = A.

    With no live line left (each closed short, reduced to zero or rejected) nothing remains
    to deliver, so the request closes too.
    """
    return all(ledger.fully_delivered for ledger in ledgers if is_live(ledger.line))


def auto_reopen_eligible(request: MaterialRequest, ledgers: Iterable[LineLedger]) -> bool:
    """``CLOSED → APPROVED`` (auto): an automatic close, and a live line with D < A."""
    return request.close_mode == MaterialRequest.CloseMode.AUTO and any(
        is_live(ledger.line) and not ledger.fully_delivered for ledger in ledgers
    )


# --- deactivation preconditions (S9, F62) ---------------------------------------------


def requests_with_outstanding(branch_ids: Collection[UUID]) -> list[OpenDocument]:
    """Approved requests of these branches with an outstanding quantity on any line.

    A request belongs to its procurement branch and to its project's branch: deactivating
    either would strand its remaining deliveries.
    """
    lines = (
        MaterialRequestLine.objects.filter(open_q())
        .filter(
            Q(request__procurement_branch_id__in=branch_ids)
            | Q(request__project__branch_id__in=branch_ids)
        )
        .select_related("request")
    )
    return sorted(
        {
            OpenDocument(
                "material_request", str(ledger.line.request.number), ledger.line.request.pk
            )
            for ledger in line_ledgers(lines)
            if ledger.outstanding > 0
        }
    )

"""Splitting a receipt line's accepted quantity over allocations (plan §4.5; F65, F69, Q14).

Pure computation over facts the service reads under the component's locks.

- **Candidates** of a receipt line: the allocations of its order line whose request line is
  open and whose ceiling remaining (``qty - attributed``) is positive, in attribution
  order. At a worksite only those of the worksite's project are candidates.
- **Caps**: a candidate takes at most min(its ceiling remaining, its request line's receipt
  capacity A - R); at a worksite also A - D, because the goods are handed over at once. R
  counts the attributions on every order (F69) and D every posted handover, and both grow
  while a receipt is split, so two allocations of one request line share its capacity.
  Without the cap, goods from a replacement order and from the original order could both
  be attributed, pushing R above A (E13).
- **Default split** (Q14): candidates in attribution order, each taking what it can. At a
  worksite the goods are physically there, so the waterfall's pending does not matter
  (E12): a later allocation of the site's project takes what an earlier one of another
  project would have had pending.
- **Manual split**: the user's parts, validated against the same caps and never more than
  was accepted.

What no candidate takes is the line's surplus: unallocated stock at a warehouse, flagged for
the material manager at a worksite.
"""

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal
from uuid import UUID

from django.core.exceptions import ValidationError
from django.utils.translation import gettext_lazy as _

ZERO = Decimal("0")


@dataclass(frozen=True)
class Candidate:
    allocation_id: UUID
    request_line_id: UUID
    ceiling_remaining: Decimal


class Capacity:
    """A, R and D of the request lines one receipt reaches; R and D grow as it is split."""

    def __init__(
        self,
        approved: Mapping[UUID, Decimal],
        received: Mapping[UUID, Decimal],
        delivered: Mapping[UUID, Decimal],
        *,
        site: bool,
    ) -> None:
        self.approved = dict(approved)
        self.received = dict(received)
        self.delivered = dict(delivered)
        self.site = site

    def cap(self, candidate: Candidate) -> Decimal:
        """The most ``candidate`` can still take from this receipt."""
        line = candidate.request_line_id
        approved = self.approved.get(line, ZERO)
        room = min(candidate.ceiling_remaining, approved - self.received.get(line, ZERO))
        if self.site:
            room = min(room, approved - self.delivered.get(line, ZERO))
        return max(ZERO, room)

    def take(self, candidate: Candidate, quantity: Decimal) -> None:
        line = candidate.request_line_id
        self.received[line] = self.received.get(line, ZERO) + quantity
        if self.site:
            self.delivered[line] = self.delivered.get(line, ZERO) + quantity


def default_split(
    quantity: Decimal, candidates: Iterable[Candidate], capacity: Capacity
) -> dict[UUID, Decimal]:
    """Candidates in the order given (attribution order) each take what they can."""
    split: dict[UUID, Decimal] = {}
    left = quantity
    for candidate in candidates:
        take = min(capacity.cap(candidate), left)
        if take > 0:
            split[candidate.allocation_id] = take
            capacity.take(candidate, take)
            left -= take
    return split


def _quantity(value: Decimal) -> str:
    return f"{value.normalize():f}"


def manual_split(
    quantity: Decimal,
    parts: Mapping[UUID, Decimal],
    candidates: Sequence[Candidate],
    capacity: Capacity,
) -> dict[UUID, Decimal]:
    """The user's ``parts`` (zero parts are dropped), checked against the caps (§4.5).

    Parts are applied in the candidates' order, so the outcome never depends on how the
    caller ordered them. An allocation that is not a candidate is refused.
    """
    by_id = {candidate.allocation_id: candidate for candidate in candidates}
    if unknown := set(parts) - set(by_id):
        raise ValidationError(
            _("%(count)s of the chosen allocations cannot take goods from this line."),
            code="not_candidate",
            params={"count": len(unknown)},
        )
    split: dict[UUID, Decimal] = {}
    for candidate in candidates:
        part = parts.get(candidate.allocation_id, ZERO)
        if part <= 0:
            continue
        available = capacity.cap(candidate)
        if part > available:
            raise ValidationError(
                _("An allocation can take at most %(available)s of these goods."),
                code="exceeds_capacity",
                params={"available": _quantity(available)},
            )
        split[candidate.allocation_id] = part
        capacity.take(candidate, part)
    if sum(split.values(), ZERO) > quantity:
        raise ValidationError(
            _("The split attributes more than the %(accepted)s accepted."),
            code="exceeds_accepted",
            params={"accepted": _quantity(quantity)},
        )
    return split

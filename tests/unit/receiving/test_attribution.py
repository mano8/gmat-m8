"""The split of an accepted quantity over allocations (plan §4.5), as pure functions."""

from decimal import Decimal
from uuid import UUID, uuid4

import pytest
from django.core.exceptions import ValidationError
from hypothesis import given
from hypothesis import strategies as st

from apps.receiving.attribution import Candidate, Capacity, default_split, manual_split
from tests.support import error_codes

D = Decimal
X, Y = uuid4(), uuid4()


def capacity(*, site: bool = False, **lines: tuple[str, str, str]) -> Capacity:
    """``x=("10", "2", "0")``: request line X with A, R and D."""
    ids = {"x": X, "y": Y}
    return Capacity(
        {ids[name]: D(a) for name, (a, _r, _d) in lines.items()},
        {ids[name]: D(r) for name, (_a, r, _d) in lines.items()},
        {ids[name]: D(d) for name, (_a, _r, d) in lines.items()},
        site=site,
    )


def candidate(line: UUID, ceiling: str) -> Candidate:
    return Candidate(uuid4(), line, D(ceiling))


class TestDefaultSplit:
    def test_in_the_order_given(self) -> None:
        first, second = candidate(X, "10"), candidate(Y, "10")
        split = default_split(
            D(15), [first, second], capacity(x=("10", "0", "0"), y=("10", "0", "0"))
        )
        assert split == {first.allocation_id: 10, second.allocation_id: 5}

    def test_capped_by_the_receipt_capacity(self) -> None:
        first = candidate(X, "10")
        split = default_split(D(10), [first], capacity(x=("10", "7", "0")))
        assert split == {first.allocation_id: 3}

    def test_on_site_also_by_the_outstanding_quantity(self) -> None:
        first = candidate(X, "10")
        on_site = capacity(site=True, x=("10", "2", "6"))
        assert default_split(D(10), [first], on_site) == {first.allocation_id: 4}
        assert (on_site.received[X], on_site.delivered[X]) == (D(6), D(10))

    def test_two_allocations_of_one_line_share_its_capacity(self) -> None:
        first, second = candidate(X, "8"), candidate(X, "8")
        split = default_split(D(16), [first, second], capacity(x=("10", "0", "0")))
        assert split == {first.allocation_id: 8, second.allocation_id: 2}


class TestManualSplit:
    def test_zero_parts_are_dropped_and_the_order_is_the_candidates(self) -> None:
        first, second = candidate(X, "10"), candidate(Y, "10")
        parts = {second.allocation_id: D(4), first.allocation_id: D(0)}
        split = manual_split(
            D(5), parts, [first, second], capacity(x=("10", "0", "0"), y=("10", "0", "0"))
        )
        assert split == {second.allocation_id: 4}

    def test_a_part_over_the_cap(self) -> None:
        first = candidate(X, "10")
        with pytest.raises(ValidationError) as exc:
            manual_split(D(5), {first.allocation_id: D(5)}, [first], capacity(x=("10", "6", "0")))
        assert error_codes(exc) == {"exceeds_capacity"}

    def test_a_stranger(self) -> None:
        with pytest.raises(ValidationError) as exc:
            manual_split(D(5), {uuid4(): D(1)}, [candidate(X, "10")], capacity(x=("10", "0", "0")))
        assert error_codes(exc) == {"not_candidate"}

    def test_more_than_accepted(self) -> None:
        first, second = candidate(X, "10"), candidate(Y, "10")
        parts = {first.allocation_id: D(3), second.allocation_id: D(3)}
        with pytest.raises(ValidationError) as exc:
            manual_split(
                D(5), parts, [first, second], capacity(x=("10", "0", "0"), y=("10", "0", "0"))
            )
        assert error_codes(exc) == {"exceeds_accepted"}


quantities = st.integers(min_value=0, max_value=30).map(Decimal)


@given(
    accepted=quantities,
    ceilings=st.lists(st.integers(min_value=1, max_value=15).map(Decimal), min_size=1, max_size=4),
    lines=st.lists(st.sampled_from([X, Y]), min_size=4, max_size=4),
    approved=st.tuples(quantities, quantities),
    received=st.tuples(quantities, quantities),
    delivered=st.tuples(quantities, quantities),
    site=st.booleans(),
)
def test_the_caps_always_hold(
    accepted: Decimal,
    ceilings: list[Decimal],
    lines: list[UUID],
    approved: tuple[Decimal, Decimal],
    received: tuple[Decimal, Decimal],
    delivered: tuple[Decimal, Decimal],
    site: bool,
) -> None:
    """Never more than accepted, than a ceiling, or past A for R (and D on site): I6, I13."""
    a = {X: approved[0], Y: approved[1]}
    r = {X: min(received[0], a[X]), Y: min(received[1], a[Y])}
    d = {X: min(delivered[0], a[X]), Y: min(delivered[1], a[Y])}
    pairs = zip(lines, ceilings, strict=False)  # as many candidates as ceilings
    candidates = [Candidate(uuid4(), line, ceiling) for line, ceiling in pairs]
    book = Capacity(a, r, d, site=site)
    split = default_split(accepted, candidates, book)
    assert sum(split.values(), Decimal(0)) <= accepted
    for item in candidates:
        assert 0 < split.get(item.allocation_id, Decimal(1)) <= item.ceiling_remaining
    for line in (X, Y):
        taken = sum(
            (
                split.get(c.allocation_id, Decimal(0))
                for c in candidates
                if c.request_line_id == line
            ),
            Decimal(0),
        )
        assert r[line] + taken <= a[line]
        if site:
            assert d[line] + taken <= a[line]
    # Nothing is left as surplus while a candidate could still take some of it.
    if sum(split.values(), Decimal(0)) < accepted:
        for item in candidates:
            left = item.ceiling_remaining - split.get(item.allocation_id, Decimal(0))
            assert min(left, book.cap(item)) == 0

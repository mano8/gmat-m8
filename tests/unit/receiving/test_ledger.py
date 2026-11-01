"""What receipts and handovers contribute to the ledger: posted documents only (plan §4.5)."""

from decimal import Decimal

import pytest

from apps.access.principals import SessionPrincipal
from apps.handovers.ledger import delivered
from apps.purchasing.receipts import LineReceipts, attributed, line_receipts
from apps.receiving import services
from apps.receiving.ledger import SOURCE
from tests.org import Org
from tests.unit.purchasing.conftest import Buy, MakeLine, allocation, only_line
from tests.unit.receiving.conftest import Receive

pytestmark = pytest.mark.django_db

D = Decimal


def test_receipts_count_until_voided(
    org: Org, make_line: MakeLine, buy: Buy, receive: Receive, mm: SessionPrincipal
) -> None:
    line = make_line("10")
    order = buy((line, "10"))
    po_line, share = only_line(order), allocation(order, line)
    first = receive(order, "4", at=org.worksite(org.hospital))
    receive(order, "3")
    assert line_receipts([po_line.pk]) == {po_line.pk: LineReceipts(D(7), posted=True)}
    assert attributed([share.pk]) == {share.pk: D(7)}
    assert delivered([line.pk])[line.pk].delivered == 4
    services.void_receipt(mm, first.pk, reason="Wrong site")
    assert line_receipts([po_line.pk])[po_line.pk].received == 3
    assert attributed([share.pk]) == {share.pk: D(3)}
    assert delivered([line.pk]) == {}


def test_nothing_received_or_delivered(make_line: MakeLine, buy: Buy) -> None:
    line = make_line("10")
    order = buy((line, "10"))
    assert SOURCE.lines([only_line(order).pk]) == {}
    assert SOURCE.attributed([allocation(order, line).pk]) == {}
    assert delivered([line.pk]) == {}
    assert delivered([]) == {}

"""Scope of receipts and handovers (S5, receipt.view ○ site) and their state tables."""

from typing import Any

import pytest
from django.core.exceptions import ValidationError

from apps.access.context import AccessContext
from apps.access.roles import Capability
from apps.accounts.models import User
from apps.handovers import transitions as handover_transitions
from apps.handovers.models import Handover, HandoverLine
from apps.receiving import transitions as receipt_transitions
from apps.receiving.models import GoodsReceipt, GoodsReceiptLine, ReceiptAttribution
from tests.org import Org, People
from tests.support import error_codes
from tests.unit.purchasing.conftest import Buy, MakeLine
from tests.unit.receiving.conftest import Receive

pytestmark = pytest.mark.django_db

RECEIPTS, HANDOVERS = Capability.RECEIPT_VIEW, Capability.HANDOVER_VIEW
MODELS = [
    (GoodsReceipt, RECEIPTS),
    (GoodsReceiptLine, RECEIPTS),
    (ReceiptAttribution, RECEIPTS),
    (Handover, HANDOVERS),
    (HandoverLine, HANDOVERS),
]


@pytest.fixture
def received(org: Org, make_line: MakeLine, buy: Buy, receive: Receive) -> dict[str, GoodsReceipt]:
    """A receipt at A-MAD's warehouse and one at the hospital's worksite."""
    order = buy((make_line("10"), "10"))
    return {
        "warehouse": receive(order, "3"),
        "site": receive(order, "4", at=org.worksite(org.hospital)),
    }


def visible(user: User, model: Any, capability: str) -> int:
    return int(model.objects.visible_to(AccessContext.for_user(user), capability).count())


@pytest.mark.parametrize(("model", "capability"), MODELS)
def test_the_order_branch_sees_every_receipt(
    people: People, received: dict[str, GoodsReceipt], model: type, capability: str
) -> None:
    expected = 1 if model in {Handover, HandoverLine} else 2
    assert visible(people.wo_mad, model, capability) == expected
    assert visible(people.group_admin, model, capability) == expected
    assert visible(people.mm_sev, model, capability) == 0


@pytest.mark.parametrize(("model", "capability"), MODELS)
def test_the_site_manager_sees_the_site_receipt_only(
    people: People, received: dict[str, GoodsReceipt], model: type, capability: str
) -> None:
    assert visible(people.site_manager, model, capability) == 1
    assert visible(people.revoked_assignment, model, capability) == 0
    assert visible(people.tech_mad, model, capability) == 0


def test_names(received: dict[str, GoodsReceipt]) -> None:
    receipt = received["site"]
    line = receipt.lines.get()
    attribution = line.attributions.get()
    handover = Handover.objects.get(goods_receipt=receipt)
    assert str(receipt) == receipt.number
    assert str(line) == f"{receipt.number} · {line.po_line}"
    assert str(attribution) == f"{line} → {attribution.allocation}"
    assert str(handover) == handover.number
    assert str(handover.lines.get()).startswith(f"{handover.number} · ")


def test_a_voided_receipt_or_handover_is_final(received: dict[str, GoodsReceipt]) -> None:
    receipt = received["site"]
    receipt.status = GoodsReceipt.Status.VOIDED
    with pytest.raises(ValidationError) as exc:
        receipt_transitions.transition(receipt, GoodsReceipt.Status.VOIDED, "void")
    assert error_codes(exc) == {"transition"}
    handover = Handover.objects.get(goods_receipt=receipt)
    handover.status = Handover.Status.VOIDED
    with pytest.raises(ValidationError) as exc:
        handover_transitions.transition(handover, Handover.Status.VOIDED, "with_receipt")
    assert error_codes(exc) == {"transition"}

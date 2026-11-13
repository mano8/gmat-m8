"""Scope of receipts and handovers (S5, receipt.view ○ site, Q44) and their state tables."""

from typing import Any

import pytest
from django.core.exceptions import ValidationError

from apps.access.context import AccessContext
from apps.access.roles import Capability, Role
from apps.accounts.models import User
from apps.handovers import transitions as handover_transitions
from apps.handovers.models import Handover, HandoverLine
from apps.projects.models import Project
from apps.receiving import transitions as receipt_transitions
from apps.receiving.models import GoodsReceipt, GoodsReceiptLine, ReceiptAttribution
from tests.org import Org, People, member
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


@pytest.mark.parametrize(("model", "capability"), MODELS)
def test_technicians_and_partners_see_the_site_receipt_only(
    people: People, received: dict[str, GoodsReceipt], model: type, capability: str
) -> None:
    """○ site (Q44): the receipt at a worksite of their project, never a handover, never a
    warehouse's receipt; nothing through an expired membership or an inactive company."""
    expected = 1 if capability == RECEIPTS else 0
    assert visible(people.tech_mad, model, capability) == expected
    assert visible(people.partner, model, capability) == expected
    for nobody in (people.expired, people.inactive_partner, people.inactive_branch):
        assert visible(nobody, model, capability) == 0


@pytest.mark.parametrize(("model", "capability"), MODELS[:3])
def test_a_project_that_is_not_active_leaves_technicians_and_partners_only(
    org: Org, people: People, received: dict[str, GoodsReceipt], model: type, capability: str
) -> None:
    """Technicians and partners reach active projects only; the site manager keeps it."""
    Project.objects.filter(pk=org.hospital.pk).update(status=Project.Status.ON_HOLD)
    assert visible(people.tech_mad, model, capability) == 0
    assert visible(people.partner, model, capability) == 0
    assert visible(people.site_manager, model, capability) == 1


@pytest.mark.parametrize(("model", "capability"), MODELS[:3])
def test_the_projects_own_branch_sees_its_site_receipts(
    org: Org, received: dict[str, GoodsReceipt], model: type, capability: str
) -> None:
    """The hospital is executed by B-MAD and bought by A-MAD: B-MAD's warehouse operator
    and material manager read the receipt at the hospital's worksite (Q44), not A-MAD's
    warehouse receipt; voiding stays with the order's branch."""
    operator = member(Role.WAREHOUSE_OPERATOR, email="wo-bmad@example.org", branch=org.b_mad)
    manager = member(Role.MATERIAL_MANAGER, email="mm-bmad@example.org", branch=org.b_mad)
    assert visible(operator, model, capability) == 1
    assert visible(manager, model, capability) == 1
    assert visible(manager, model, Capability.RECEIPT_VOID) == 0


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

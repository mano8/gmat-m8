"""Project receipts against the quantity ledger (M9.3, Q44, ADR-0028).

A technician or a partner posts goods somebody else requested; the ledger rules stay the
same as for any receiver: the order line's remaining quantity (I4), the allocation ceilings
A - R and A - D, surplus on site, one linked direct handover, an automatic close, and a void
(only the managers') that reopens what the receipt closed. After each scenario the
independent checker (``tests/invariants.py``) finds no broken rule.
"""

from decimal import Decimal

import pytest
from django.core.exceptions import ValidationError
from django.utils import translation

from apps.access.context import AccessContext
from apps.access.exceptions import AccessDenied, NotInScope
from apps.catalog.models import Material
from apps.core.idempotency import IdempotencyConflict, IdempotencyKeyInvalid, issue_key
from apps.handovers import services as handovers
from apps.handovers.models import Handover
from apps.handovers.services import HandoverLineInput, Receiver
from apps.material_requests.models import MaterialRequest
from apps.purchasing.models import PurchaseOrder
from apps.receiving import services
from apps.receiving.models import GoodsReceipt, ReceiptAttribution
from apps.receiving.services import ReceiptLineInput
from tests.invariants import violations
from tests.org import Org, People
from tests.support import error_codes, principal_for
from tests.unit.purchasing.conftest import Buy, MakeLine, flows, only_line
from tests.unit.receiving.test_project_receiving import post, refused_without_writes, snapshot

pytestmark = pytest.mark.django_db

D = Decimal
RECEIVERS = ["tech_mad", "partner"]


def broken_rules(people: People) -> list[str]:
    return [str(found) for found in violations(AccessContext.for_user(people.group_admin))]


def status_of(order: PurchaseOrder) -> str:
    return str(PurchaseOrder.objects.get(pk=order.pk).status)


class TestQuantityCaps:
    @pytest.mark.parametrize("actor", RECEIVERS)
    def test_no_more_than_the_order_line_can_still_receive(
        self, org: Org, people: People, make_line: MakeLine, buy: Buy, actor: str
    ) -> None:
        order = buy((make_line("10"), "10"))
        too_much = ReceiptLineInput(only_line(order).pk, D(11))
        caught = refused_without_writes(
            ValidationError,
            principal_for(getattr(people, actor)),
            order,
            org.worksite(org.hospital),
            too_much,
        )
        assert error_codes(caught.value) == {"over_receipt"}
        assert broken_rules(people) == []

    @pytest.mark.parametrize("actor", ["tech_mad", "partner", "site_manager"])
    def test_a_refusal_never_says_which_line_did_not_fit(
        self, org: Org, people: People, make_line: MakeLine, buy: Buy, breaker: Material, actor: str
    ) -> None:
        """Both lines serve the hospital; the cable line is shared with SEV-2. A probe on it
        beside a quantity the breaker line cannot take is refused the same way whether the
        probe fits or not, so repeated posts that write nothing do not find what remains on
        the shared line (§3.4)."""
        cable = make_line("10")
        breakers = make_line("4", material=breaker)
        order = buy(
            (cable, "10"), (make_line("10", project=org.sev_for_mad), "10"), (breakers, "4")
        )
        cable_line, breaker_line = order.lines.order_by("line_no")
        principal, site = principal_for(getattr(people, actor)), org.worksite(org.hospital)
        answers = set()
        for probe in ("5", "15"):  # fits the shared line (20), or not
            caught = refused_without_writes(
                ValidationError,
                principal,
                order,
                site,
                ReceiptLineInput(cable_line.pk, D(probe)),
                ReceiptLineInput(breaker_line.pk, D(9)),
            )
            assert isinstance(caught.value, ValidationError)
            with translation.override("en"):
                messages = tuple(str(text) for text in caught.value.messages)
            answers.add((messages, frozenset(error_codes(caught.value))))
        assert len(answers) == 1
        (messages, codes), *_ = answers
        assert codes == {"over_receipt"} and not any("Line" in text for text in messages)

    def test_who_sees_the_order_is_told_which_line(
        self, org: Org, people: People, make_line: MakeLine, buy: Buy, breaker: Material
    ) -> None:
        order = buy((make_line("10"), "10"), (make_line("4", material=breaker), "4"))
        _cable_line, breaker_line = order.lines.order_by("line_no")
        caught = refused_without_writes(
            ValidationError,
            principal_for(people.wo_mad),
            order,
            org.worksite(org.hospital),
            ReceiptLineInput(breaker_line.pk, D(9)),
        )
        assert error_codes(caught.value) == {"over_receipt"}
        assert isinstance(caught.value, ValidationError)
        with translation.override("en"):
            assert any("Line 2" in str(text) for text in caught.value.messages)

    def test_two_receivers_use_the_order_up_and_a_third_receipt_is_refused(
        self, org: Org, people: People, make_line: MakeLine, buy: Buy
    ) -> None:
        line = make_line("10")
        order = buy((line, "10"))
        site = org.worksite(org.hospital)
        first = post(
            principal_for(people.tech_mad),
            order,
            site,
            ReceiptLineInput(only_line(order).pk, D(4)),
        )
        assert status_of(order) == "ISSUED"
        second = post(
            principal_for(people.partner),
            order,
            site,
            ReceiptLineInput(only_line(order).pk, D(6)),
        )
        assert [r.received_by for r in (first, second)] == [people.tech_mad, people.partner]
        assert {
            h.goods_receipt_id: (h.handed_by, h.received_by) for h in Handover.objects.all()
        } == {
            first.pk: (people.tech_mad, people.tech_mad),
            second.pk: (people.partner, people.partner),
        }
        assert (status_of(order), MaterialRequest.objects.get().status) == ("CLOSED", "CLOSED")
        assert flows(line).received == flows(line).delivered == D(10)
        # The order is closed: nobody receives on it any more, and nothing is written.
        for actor in RECEIVERS:
            caught = refused_without_writes(
                ValidationError, principal_for(getattr(people, actor)), order, site
            )
            assert error_codes(caught.value) == {"not_receivable"}
        assert broken_rules(people) == []

    def test_the_ceilings_leave_the_excess_on_site_as_surplus(
        self, org: Org, people: People, make_line: MakeLine, buy: Buy
    ) -> None:
        """An order line of 20 for the hospital (10) and SEV-2 (10). The hospital already
        has 6 received at the warehouse and handed over (R = D = 6): the technician's 14
        at its worksite attribute 4 (A - R, A - D), the other 10 are surplus, and SEV-2's
        allocation is not touched."""
        manager = principal_for(people.mm_mad)
        hospital = make_line("10")
        sev = make_line("10", project=org.sev_for_mad)
        order = buy((hospital, "10"), (sev, "10"))
        madrid = org.warehouses["A Madrid"]
        post(manager, order, madrid, ReceiptLineInput(only_line(order).pk, D(6)))
        handovers.create_handover(
            manager,
            org.hospital.pk,
            source_location_id=madrid.pk,
            lines=[HandoverLineInput(hospital.pk, D(6))],
            receiver=Receiver(name="Site crew"),
        )
        receipt = post(
            principal_for(people.tech_mad),
            order,
            org.worksite(org.hospital),
            ReceiptLineInput(only_line(order).pk, D(14)),
        )
        line = receipt.lines.get()
        assert (line.qty_received, line.surplus_qty) == (D(14), D(10))
        assert list(
            ReceiptAttribution.objects.filter(receipt_line=line).values_list("qty", flat=True)
        ) == [D(4)]
        assert (flows(hospital).received, flows(hospital).delivered) == (D(10), D(10))
        assert (flows(sev).received, flows(sev).delivered) == (D(0), D(0))
        assert Handover.objects.get(goods_receipt=receipt).lines.get().qty == D(4)
        assert broken_rules(people) == []


class TestDuplicateSubmission:
    @pytest.mark.parametrize("actor", RECEIVERS)
    def test_the_same_key_makes_one_receipt_and_one_handover(
        self, org: Org, people: People, make_line: MakeLine, buy: Buy, actor: str
    ) -> None:
        user = getattr(people, actor)
        order = buy((make_line("10"), "10"))
        principal, site = principal_for(user), org.worksite(org.hospital)
        key = issue_key(user.pk, "receipt.post")
        first = post(principal, order, site, idempotency_key=key)
        again = post(principal, order, site, idempotency_key=key)
        assert again.pk == first.pk
        assert (GoodsReceipt.objects.count(), Handover.objects.count()) == (1, 1)
        assert flows(MaterialRequest.objects.get().lines.get()).received == D(4)

    def test_the_same_key_with_another_payload_is_a_conflict(
        self, org: Org, people: People, make_line: MakeLine, buy: Buy
    ) -> None:
        order = buy((make_line("10"), "10"))
        principal, site = principal_for(people.tech_mad), org.worksite(org.hospital)
        key = issue_key(people.tech_mad.pk, "receipt.post")
        post(principal, order, site, idempotency_key=key)
        before = snapshot()
        with pytest.raises(IdempotencyConflict):
            post(
                principal,
                order,
                site,
                ReceiptLineInput(only_line(order).pk, D(5)),
                idempotency_key=key,
            )
        assert snapshot() == before

    def test_a_key_is_the_users_own(
        self, org: Org, people: People, make_line: MakeLine, buy: Buy
    ) -> None:
        """The partner cannot replay the technician's key and read their receipt."""
        order = buy((make_line("10"), "10"))
        site = org.worksite(org.hospital)
        key = issue_key(people.tech_mad.pk, "receipt.post")
        post(principal_for(people.tech_mad), order, site, idempotency_key=key)
        before = snapshot()
        with pytest.raises(IdempotencyKeyInvalid, match="another user"):
            post(principal_for(people.partner), order, site, idempotency_key=key)
        assert snapshot() == before


class TestVoid:
    """A project receiver cannot void; the managers can, and what the receipt closed
    reopens (§4.9)."""

    @pytest.mark.parametrize("actor", RECEIVERS)
    def test_the_receiver_cannot_void_their_receipt(
        self, org: Org, people: People, make_line: MakeLine, buy: Buy, actor: str
    ) -> None:
        order = buy((make_line("10"), "10"))
        principal = principal_for(getattr(people, actor))
        receipt = post(principal, order, org.worksite(org.hospital))
        before = snapshot()
        with pytest.raises((AccessDenied, NotInScope)):
            services.void_receipt(principal, receipt.pk, reason="Mistake")
        assert snapshot() == before
        assert GoodsReceipt.objects.get().status == GoodsReceipt.Status.POSTED

    @pytest.mark.parametrize("voider", ["mm_mad", "engineer", "group_admin"])
    @pytest.mark.parametrize("actor", RECEIVERS)
    def test_a_manager_voids_it_and_the_ledger_goes_back(
        self,
        org: Org,
        people: People,
        make_line: MakeLine,
        buy: Buy,
        actor: str,
        voider: str,
    ) -> None:
        line = make_line("10")
        order = buy((line, "10"))
        receipt = post(
            principal_for(getattr(people, actor)),
            order,
            org.worksite(org.hospital),
            ReceiptLineInput(only_line(order).pk, D(10)),
        )
        assert (status_of(order), MaterialRequest.objects.get().status) == ("CLOSED", "CLOSED")
        services.void_receipt(principal_for(getattr(people, voider)), receipt.pk, reason="Wrong")
        assert GoodsReceipt.objects.get().status == GoodsReceipt.Status.VOIDED
        assert Handover.objects.get().status == Handover.Status.VOIDED
        assert (status_of(order), MaterialRequest.objects.get().status) == ("ISSUED", "APPROVED")
        assert (flows(line).received, flows(line).delivered) == (D(0), D(0))
        assert broken_rules(people) == []
        # Correcting it is a new receipt, by the same receiver.
        post(
            principal_for(getattr(people, actor)),
            order,
            org.worksite(org.hospital),
            ReceiptLineInput(only_line(order).pk, D(10)),
        )
        assert flows(line).received == D(10)
        assert broken_rules(people) == []

    def test_a_void_revives_the_pending_that_the_receipt_had_used_up(
        self, org: Org, people: People, make_line: MakeLine, buy: Buy
    ) -> None:
        """Two requests share one order line of 10: the technician's receipt of 10 serves
        the first (HIGH); after the void the first is pending again, not the second."""
        first = make_line("6", priority=MaterialRequest.Priority.HIGH)
        second = make_line("4")
        order = buy((first, "6"), (second, "4"))
        receipt = post(
            principal_for(people.tech_mad),
            order,
            org.worksite(org.hospital),
            ReceiptLineInput(only_line(order).pk, D(10)),
        )
        assert (flows(first).received, flows(second).received) == (D(6), D(4))
        services.void_receipt(principal_for(people.mm_mad), receipt.pk, reason="Wrong")
        assert (flows(first).pending, flows(second).pending) == (D(6), D(4))
        assert broken_rules(people) == []

"""Project receiving for every role (Q44, ADR-0028), through ``post_receipt``.

Every business role receives at the worksite of a project in its membership's scope,
whoever requested the goods. A-MAD's technician reaches the hospital (B-MAD, managed by
A-MAD) and SEV-2 (bought by A-MAD); the partner of C reaches the hospital only. What a
project receiver may name is the project's worksite, the order lines that serve the
project and the project's allocations: anything else is refused and writes nothing.
"""

from decimal import Decimal

import pytest
from django.core.exceptions import ValidationError
from django.db.models import F
from django.utils import timezone

from apps.access.exceptions import AccessDenied, NotInScope, StaleSession
from apps.access.principals import SessionPrincipal
from apps.access.roles import Role
from apps.accounts.models import User
from apps.audit.models import AuditEvent
from apps.catalog.models import Material
from apps.core.idempotency import issue_key
from apps.handovers.models import Handover, HandoverLine
from apps.locations.models import Location
from apps.material_requests.models import MaterialRequest, MaterialRequestLine
from apps.organizations.models import Membership
from apps.projects.models import Project, ProjectAssignment
from apps.purchasing import services as purchasing
from apps.purchasing.models import PurchaseOrder
from apps.purchasing.services import RequestItem
from apps.receiving import services
from apps.receiving.models import GoodsReceipt, ReceiptAttribution
from apps.receiving.services import ReceiptLineInput
from tests.org import Org, People, assign, member
from tests.support import error_codes, principal_for
from tests.unit.purchasing.conftest import Buy, MakeLine, allocation, flows, only_line, q

pytestmark = pytest.mark.django_db

D = Decimal
RECEIVERS = ["tech_mad", "partner"]


def requested_by(line: MaterialRequestLine, user: User) -> MaterialRequestLine:
    """The line's request, as if ``user`` had made it."""
    MaterialRequest.objects.filter(pk=line.request_id).update(requested_by=user)
    return line


def snapshot() -> tuple[object, ...]:
    return (
        GoodsReceipt.objects.count(),
        ReceiptAttribution.objects.count(),
        Handover.objects.count(),
        AuditEvent.objects.filter(action__startswith="receipt.").count(),
    )


def post(
    principal: SessionPrincipal,
    order: PurchaseOrder,
    at: Location,
    *lines: ReceiptLineInput,
    **kwargs: object,
) -> GoodsReceipt:
    return services.post_receipt(
        principal,
        order.pk,
        location_id=at.pk,
        lines=list(lines) or [ReceiptLineInput(order.lines.order_by("line_no")[0].pk, D(4))],
        **kwargs,  # type: ignore[arg-type]
    )


def refused_without_writes(
    error: type[Exception] | tuple[type[Exception], ...],
    principal: SessionPrincipal,
    order: PurchaseOrder,
    at: Location,
    *lines: ReceiptLineInput,
) -> pytest.ExceptionInfo[Exception]:
    before = snapshot()
    with pytest.raises(error) as caught:
        post(principal, order, at, *lines)
    assert snapshot() == before
    return caught


@pytest.fixture
def stranger(org: Org) -> User:
    """Who requested the goods: somebody none of the receivers ever met."""
    return member(Role.TECHNICIAN, email="stranger@example.org", branch=org.a_sev)


class TestTheReceiversOfAProject:
    @pytest.mark.parametrize("actor", RECEIVERS)
    def test_receives_goods_someone_else_requested(
        self,
        org: Org,
        people: People,
        make_line: MakeLine,
        buy: Buy,
        stranger: User,
        actor: str,
    ) -> None:
        user = getattr(people, actor)
        line = requested_by(make_line("10"), stranger)
        order = buy((line, "10"))
        receipt = post(principal_for(user, mfa_at=None), order, org.worksite(org.hospital))
        assert (receipt.received_by, receipt.location) == (user, org.worksite(org.hospital))
        assert receipt.branch == org.a_mad  # still a document of the order's branch (S5)
        assert {a.allocation for a in ReceiptAttribution.objects.all()} == {allocation(order, line)}
        handover = Handover.objects.get(goods_receipt=receipt)  # exactly one, linked
        assert handover.kind == Handover.Kind.DIRECT_RECEIPT
        assert (handover.handed_by, handover.received_by) == (user, user)
        assert list(HandoverLine.objects.values_list("qty", flat=True)) == [D(4)]
        assert (flows(line).received, flows(line).delivered) == q(4, 4)
        actors = set(
            AuditEvent.objects.filter(action__in=["receipt.posted", "handover.posted"]).values_list(
                "actor", flat=True
            )
        )
        assert actors == {user.pk}

    def test_the_project_of_another_branch_bought_by_theirs(
        self, org: Org, people: People, make_line: MakeLine, buy: Buy
    ) -> None:
        """SEV-2 is executed by A-SEV and bought by A-MAD: A-MAD's technician reaches it
        through the procurement branch (S3)."""
        order = buy((make_line("10", project=org.sev_for_mad), "10"))
        post(principal_for(people.tech_mad), order, org.worksite(org.sev_for_mad))
        assert GoodsReceipt.objects.count() == 1

    @pytest.mark.parametrize("role", [Role.WAREHOUSE_OPERATOR, Role.MATERIAL_MANAGER])
    def test_the_branch_that_executes_the_project(
        self, org: Org, make_line: MakeLine, buy: Buy, role: Role
    ) -> None:
        """B-MAD executes the hospital, A-MAD buys for it: B-MAD receives on the hospital's
        worksite (Q44), but neither at A-MAD's warehouse nor on A-MAD's order alone."""
        user = member(role, email="bmad@example.org", branch=org.b_mad)
        order = buy((make_line("10"), "10"))
        refused_without_writes(NotInScope, principal_for(user), order, org.warehouses["A Madrid"])
        post(principal_for(user), order, org.worksite(org.hospital))
        assert GoodsReceipt.objects.get().received_by == user

    def test_a_receipt_shared_across_projects_takes_only_this_projects_goods(
        self, org: Org, people: People, make_line: MakeLine, buy: Buy
    ) -> None:
        """One order line for the hospital and SEV-2: at the hospital the goods go to the
        hospital's allocation only, the rest is surplus on site."""
        hospital = make_line("10")
        sev = make_line("10", project=org.sev_for_mad)
        order = buy((sev, "10"), (hospital, "10"))
        item = ReceiptLineInput(only_line(order).pk, D(15))
        receipt = post(principal_for(people.partner), order, org.worksite(org.hospital), item)
        line = receipt.lines.get()
        assert (line.qty_received, line.surplus_qty) == (D(15), D(5))
        assert flows(hospital).received == D(10)
        assert flows(sev).received == 0


class TestForgedChoices:
    """What a project receiver names that is not theirs is refused and writes nothing."""

    @pytest.fixture
    def shared(self, org: Org, make_line: MakeLine, buy: Buy, breaker: Material) -> PurchaseOrder:
        """An order with a line for the hospital (cable) and one only for SEV-2 (breakers)."""
        hospital = make_line("10")
        sev = make_line("10", project=org.sev_for_mad, material=breaker)
        return buy((hospital, "10"), (sev, "10"))

    @pytest.mark.parametrize("actor", RECEIVERS)
    def test_another_projects_allocation(
        self, org: Org, people: People, make_line: MakeLine, buy: Buy, actor: str
    ) -> None:
        hospital, sev = make_line("10"), make_line("10", project=org.sev_for_mad)
        order = buy((hospital, "10"), (sev, "10"))
        forged = ReceiptLineInput(
            only_line(order).pk, D(4), split={allocation(order, sev).pk: D(4)}
        )
        caught = refused_without_writes(
            ValidationError,
            principal_for(getattr(people, actor)),
            order,
            org.worksite(org.hospital),
            forged,
        )
        assert error_codes(caught.value) == {"not_candidate"}

    @pytest.mark.parametrize("actor", RECEIVERS)
    def test_an_order_line_that_serves_only_another_project(
        self, org: Org, people: People, shared: PurchaseOrder, breaker: Material, actor: str
    ) -> None:
        """Answered like a line of another order, before its unit's precision is checked:
        the refusal says nothing about the line."""
        other = shared.lines.get(material=breaker)
        for quantity in (D(1), D("0.5")):
            caught = refused_without_writes(
                ValidationError,
                principal_for(getattr(people, actor)),
                shared,
                org.worksite(org.hospital),
                ReceiptLineInput(other.pk, quantity),
            )
            assert error_codes(caught.value) == {"unknown"}

    def test_who_sees_the_whole_order_may_record_it_as_surplus(
        self, org: Org, people: People, shared: PurchaseOrder, breaker: Material
    ) -> None:
        """Goods delivered to the wrong site (E12): the order's branch records them."""
        other = shared.lines.get(material=breaker)
        receipt = post(
            principal_for(people.wo_mad),
            shared,
            org.worksite(org.hospital),
            ReceiptLineInput(other.pk, D(3)),
        )
        assert receipt.lines.get().surplus_qty == D(3)

    def test_a_worksite_outside_the_partners_projects(
        self, org: Org, people: People, shared: PurchaseOrder
    ) -> None:
        refused_without_writes(
            NotInScope, principal_for(people.partner), shared, org.worksite(org.sev_for_mad)
        )

    @pytest.mark.parametrize("actor", RECEIVERS)
    def test_a_warehouse(self, org: Org, people: People, shared: PurchaseOrder, actor: str) -> None:
        for warehouse in org.warehouses.values():
            refused_without_writes(
                NotInScope, principal_for(getattr(people, actor)), shared, warehouse
            )

    @pytest.mark.parametrize("actor", RECEIVERS)
    def test_an_order_that_buys_nothing_for_their_projects(
        self, org: Org, people: People, mm_sev_buys: PurchaseOrder, actor: str
    ) -> None:
        for project in (org.hospital, org.sev_works):
            refused_without_writes(
                NotInScope,
                principal_for(getattr(people, actor)),
                mm_sev_buys,
                org.worksite(project),
            )

    @pytest.fixture
    def mm_sev_buys(self, org: Org, people: People, make_line: MakeLine) -> PurchaseOrder:
        """An order of A-SEV for SEV-1, a project no receiver here reaches."""
        buyer = principal_for(people.mm_sev)
        line = make_line("10", project=org.sev_works)
        order = purchasing.create_po(
            buyer,
            branch_id=org.a_sev.pk,
            supplier_id=org.supplier.pk,
            ship_to_id=org.warehouses["A Sevilla"].pk,
        )
        purchasing.add_request_lines(buyer, order.pk, [RequestItem(line.pk, D(10))])
        purchasing.issue(buyer, order.pk)
        return PurchaseOrder.objects.get(pk=order.pk)


class TestTheMembershipDecides:
    """Role alone grants nothing outside the membership's scope (S6, S8, S9)."""

    def test_a_technician_of_another_branch(self, org: Org, make_line: MakeLine, buy: Buy) -> None:
        technician = member(Role.TECHNICIAN, email="tech-sev@example.org", branch=org.a_sev)
        order = buy((make_line("10"), "10"))
        refused_without_writes(
            NotInScope, principal_for(technician), order, org.worksite(org.hospital)
        )
        # SEV-2 is executed by A-SEV: its own technicians receive there.
        sev = buy((make_line("10", project=org.sev_for_mad), "10"))
        post(principal_for(technician), sev, org.worksite(org.sev_for_mad))

    def test_a_partner_assigned_elsewhere_or_as_a_manager(
        self, org: Org, make_line: MakeLine, buy: Buy
    ) -> None:
        partner = member(Role.PARTNER_REQUESTER, email="partner-2@example.org", company=org.c)
        assign(partner, org.sev_for_mad, ProjectAssignment.Kind.PARTNER)
        assign(partner, org.hospital, ProjectAssignment.Kind.MANAGER)  # no manager membership
        order = buy((make_line("10"), "10"))
        refused_without_writes(
            NotInScope, principal_for(partner), order, org.worksite(org.hospital)
        )

    @pytest.mark.parametrize(
        "actor", ["expired", "inactive_partner", "inactive_branch", "revoked_assignment"]
    )
    def test_no_valid_grant(
        self, org: Org, people: People, make_line: MakeLine, buy: Buy, actor: str
    ) -> None:
        order = buy((make_line("10"), "10"))
        refused_without_writes(
            NotInScope, principal_for(getattr(people, actor)), order, org.worksite(org.hospital)
        )

    @pytest.mark.parametrize("status", [Project.Status.ON_HOLD, Project.Status.CLOSED])
    def test_technicians_and_partners_receive_on_active_projects_only(
        self, org: Org, people: People, make_line: MakeLine, buy: Buy, status: str
    ) -> None:
        order = buy((make_line("10"), "10"))
        Project.objects.filter(pk=org.hospital.pk).update(status=status)
        for actor in RECEIVERS:
            refused_without_writes(
                NotInScope, principal_for(getattr(people, actor)), order, org.worksite(org.hospital)
            )

    def test_an_inactive_employer_company(
        self, org: Org, people: People, make_line: MakeLine, buy: Buy
    ) -> None:
        order = buy((make_line("10"), "10"))
        org.c.is_active = False
        org.c.save(update_fields=["is_active"])
        refused_without_writes(
            NotInScope, principal_for(people.partner), order, org.worksite(org.hospital)
        )


class TestCheckedAgainOnEveryCall:
    """The write re-checks the grant inside its transaction, an idempotent replay too."""

    @pytest.mark.parametrize("actor", RECEIVERS)
    def test_a_replay_after_the_grant_ended(
        self, org: Org, people: People, make_line: MakeLine, buy: Buy, actor: str
    ) -> None:
        user = getattr(people, actor)
        order = buy((make_line("10"), "10"))
        key = issue_key(user.pk, "receipt.post")
        principal = principal_for(user)
        first = post(principal, order, org.worksite(org.hospital), idempotency_key=key)
        assert post(principal, order, org.worksite(org.hospital), idempotency_key=key) == first
        Membership.objects.filter(user=user).update(revoked_at=timezone.now())
        before = snapshot()
        with pytest.raises((NotInScope, AccessDenied)):
            post(principal, order, org.worksite(org.hospital), idempotency_key=key)
        assert snapshot() == before

    def test_a_replay_after_the_project_went_on_hold(
        self, org: Org, people: People, make_line: MakeLine, buy: Buy
    ) -> None:
        order = buy((make_line("10"), "10"))
        key = issue_key(people.tech_mad.pk, "receipt.post")
        principal = principal_for(people.tech_mad)
        post(principal, order, org.worksite(org.hospital), idempotency_key=key)
        Project.objects.filter(pk=org.hospital.pk).update(status=Project.Status.ON_HOLD)
        with pytest.raises(NotInScope):
            post(principal, order, org.worksite(org.hospital), idempotency_key=key)

    def test_a_session_older_than_a_password_change(
        self, org: Org, people: People, make_line: MakeLine, buy: Buy
    ) -> None:
        order = buy((make_line("10"), "10"))
        principal = principal_for(people.partner)
        User.objects.filter(pk=people.partner.pk).update(session_epoch=F("session_epoch") + 1)
        refused_without_writes(StaleSession, principal, order, org.worksite(org.hospital))

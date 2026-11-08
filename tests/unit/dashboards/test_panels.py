"""The dashboard panels (plan M7.2): each reads through its owning app's scoped selector, so
a panel shows the user's scope only, and the registry reaches a user's capabilities only."""

from collections.abc import Iterator
from datetime import timedelta

import pytest
from django.utils import timezone, translation

from apps.access.context import AccessContext
from apps.dashboards import panels
from apps.handovers import selectors as handover_selectors
from apps.material_requests import services
from apps.material_requests.models import MaterialRequest, MaterialRequestLine
from apps.notifications.models import Kind, Status
from apps.purchasing.models import PurchaseOrder
from tests.orders import order as make_order
from tests.org import Org, People
from tests.outbox import make_dispatch
from tests.support import every_key, principal_for, read_all
from tests.unit.purchasing.conftest import Buy, MakeLine
from tests.unit.receiving.conftest import Receive

pytestmark = pytest.mark.django_db

UNDECIDED = MaterialRequestLine.Sourcing.UNDECIDED


@pytest.fixture(autouse=True)
def english() -> Iterator[None]:
    """The panels translate when they are built: read them in English."""
    with translation.override("en"):
        yield


def keys(ctx: AccessContext) -> set[str]:
    return {panel.key for panel in panels.visible(ctx)}


class TestRegistry:
    def test_every_key_is_unique_and_resolvable(self) -> None:
        assert len({panel.key for panel in panels.PANELS}) == len(panels.PANELS)
        assert set(panels.BY_KEY) == {panel.key for panel in panels.PANELS}

    def test_requesters_get_their_requests_only(self, people: People) -> None:
        for user in (people.tech_mad, people.partner):
            assert keys(AccessContext.for_user(user)) == {"my-requests", "request-progress"}

    def test_a_site_manager_follows_requests_receipts_and_reviews(self, people: People) -> None:
        assert keys(AccessContext.for_user(people.site_manager)) == {
            "my-requests",
            "inbox",
            "site-surplus",
            "request-progress",
            "decisions",
            "inbox-age",
            "receipts-handovers",
        }

    def test_a_warehouse_operator_gets_goods_and_orders_not_purchasing(
        self, people: People
    ) -> None:
        assert keys(AccessContext.for_user(people.wo_mad)) == {
            "my-requests",
            "awaiting-receipt",
            "overdue",
            "to-hand-over",
            "site-surplus",
            "orders-receipts",
            "on-time",
            "overdue-suppliers",
            "receipts-handovers",
            "handover-projects",
        }

    def test_a_material_manager_gets_the_worklists_not_the_overview(
        self, ctx: AccessContext
    ) -> None:
        found = keys(ctx)
        assert {"sourcing", "to-purchase", "stale-drafts", "unallocated", "dispatches"} <= found
        assert {"decisions", "inbox-age", "orders-receipts", "handover-projects"} <= found
        assert not {"overview", "branch-requests", "branch-people"} & found

    def test_a_group_administrator_gets_every_panel(self, people: People) -> None:
        assert keys(AccessContext.for_user(people.group_admin)) == set(panels.BY_KEY)

    def test_nobody_gets_nothing(self, people: People) -> None:
        assert panels.visible(AccessContext.for_user(people.nobody)) == []


class TestRequesterAndReviewer:
    def test_my_requests_lists_the_open_ones_of_the_user(
        self, people: People, make_line: MakeLine
    ) -> None:
        mine = make_line("4")
        make_line("4", status=MaterialRequest.Status.CLOSED)
        MaterialRequest.objects.create(
            project=mine.project,
            procurement_branch_id=mine.project.procurement_branch_id,
            requested_by=people.tech_mad,
        )
        content = panels.my_requests(AccessContext.for_user(people.tech_mad))
        assert content.count == 2
        assert {row.label for row in content.rows} == {mine.request.number, "Draft"}

    def test_my_requests_never_lists_someone_elses(
        self, people: People, make_line: MakeLine
    ) -> None:
        make_line("4")
        assert panels.my_requests(AccessContext.for_user(people.partner)).count == 0

    def test_the_inbox_counts_submitted_requests_in_review_scope(
        self, ctx: AccessContext, other: AccessContext, make_line: MakeLine
    ) -> None:
        line = make_line("4", status=MaterialRequest.Status.SUBMITTED)
        content = panels.inbox(ctx)
        assert content.count == 1
        assert content.rows[0].label == line.request.number
        assert panels.inbox(other).count == 0


class TestAssigned:
    def test_the_panel_counts_the_submitted_requests_sent_to_the_user(
        self, people: People, make_line: MakeLine
    ) -> None:
        line = make_line("4", status=MaterialRequest.Status.SUBMITTED)
        services.assign_reviewer(
            principal_for(people.mm_mad),
            line.request_id,
            reviewer_id=people.engineer.pk,
            note="Yours",
        )
        content = panels.assigned(AccessContext.for_user(people.engineer))
        assert content.count == 1
        assert content.rows[0].label == line.request.number
        assert panels.assigned(AccessContext.for_user(people.mm_mad)).count == 0

    def test_approved_and_rejected_requests_count_too_closed_ones_do_not(
        self, people: People, make_line: MakeLine
    ) -> None:
        Status = MaterialRequest.Status
        found = {}
        for status in (Status.SUBMITTED, Status.APPROVED, Status.REJECTED, Status.CLOSED):
            line = make_line("4", status=Status.SUBMITTED)
            services.assign_reviewer(
                principal_for(people.mm_mad),
                line.request_id,
                reviewer_id=people.engineer.pk,
                note="Yours",
            )
            closed = {"closed_at": timezone.now(), "close_mode": "AUTO"}
            MaterialRequest.objects.filter(pk=line.request_id).update(
                status=status, **(closed if status == Status.CLOSED else {})
            )
            found[status] = line.request.number
        content = panels.assigned(AccessContext.for_user(people.engineer))
        assert content.count == 3
        assert {row.label for row in content.rows} == {
            found[Status.SUBMITTED],
            found[Status.APPROVED],
            found[Status.REJECTED],
        }
        assert any("Rejected" in row.detail for row in content.rows)

    def test_only_who_can_be_assigned_gets_the_panel_and_it_links_to_the_filter(
        self, people: People
    ) -> None:
        assert "assigned" in keys(AccessContext.for_user(people.engineer))
        assert "assigned" not in keys(AccessContext.for_user(people.site_manager))
        assert panels.BY_KEY["assigned"].query == "?assigned=1"
        found = panels.BY_KEY["assigned"]
        assert isinstance(found, panels.Panel)
        assert not found.alert  # rejected requests ask for no action


class TestSourcingAndPurchasing:
    def test_sourcing_lists_undecided_open_lines_in_scope(
        self, ctx: AccessContext, other: AccessContext, make_line: MakeLine
    ) -> None:
        make_line("4")
        undecided = make_line("6", sourcing=UNDECIDED)
        content = panels.sourcing(ctx)
        assert content.count == 1
        assert content.rows[0].label.startswith(undecided.request.number or "")
        assert content.rows[0].detail == "6 M"
        assert panels.sourcing(other).count == 0

    def test_to_purchase_lists_what_is_left_to_buy(
        self, ctx: AccessContext, other: AccessContext, make_line: MakeLine, buy: Buy
    ) -> None:
        bought, open_ = make_line("10"), make_line("7")
        buy((bought, "10"))
        content = panels.to_purchase(ctx)
        assert content.count == 1
        assert content.rows[0].detail == "7 M"
        assert content.rows[0].label.startswith(open_.request.number or "")
        assert panels.to_purchase(other).count == 0

    def test_a_count_worked_out_line_by_line_stops_at_the_cap(
        self,
        monkeypatch: pytest.MonkeyPatch,
        ctx: AccessContext,
        make_line: MakeLine,
        buy: Buy,
    ) -> None:
        monkeypatch.setattr(panels, "COUNT_CAP", 2)
        for _ in range(2):
            make_line("3")
        exactly = panels.to_purchase(ctx)
        assert (exactly.count, exactly.more, exactly.label) == (2, False, "2")
        make_line("3")
        capped = panels.to_purchase(ctx)
        assert (capped.count, capped.more, capped.label) == (2, True, "2+")
        assert len(capped.rows) == 2

    def test_stale_drafts_are_the_old_ones(
        self, org: Org, ctx: AccessContext, other: AccessContext
    ) -> None:
        make_order(org)
        old = make_order(org)
        PurchaseOrder.objects.filter(pk=old.pk).update(
            created_at=timezone.now() - timedelta(days=15)
        )
        content = panels.stale_drafts(ctx)
        assert content.count == 1
        assert content.rows[0].label == org.supplier.name
        assert content.rows[0].href.endswith(f"/orders/{old.pk}/")
        assert panels.stale_drafts(other).count == 0

    def test_unallocated_shows_order_quantity_for_nobody(
        self,
        org: Org,
        people: People,
        ctx: AccessContext,
        other: AccessContext,
        make_line: MakeLine,
    ) -> None:
        from tests.orders import allocate, line

        request_line = make_line("10")
        po = make_order(org, status="ISSUED", issued_by=people.mm_mad)
        assert request_line.material is not None
        po_line = line(po, request_line.material, "10")
        allocate(po_line, request_line, "6")
        content = panels.unallocated(ctx)
        assert content.count == 1
        assert content.rows[0].detail == "4 M"
        assert panels.unallocated(other).count == 0

    def test_orders_awaiting_receipt_and_overdue(
        self, org: Org, people: People, ctx: AccessContext, other: AccessContext
    ) -> None:
        today = timezone.localdate()
        late = make_order(
            org, status="ISSUED", issued_by=people.mm_mad, expected_date=today - timedelta(days=2)
        )
        soon = make_order(
            org, status="ISSUED", issued_by=people.mm_mad, expected_date=today + timedelta(days=2)
        )
        undated = make_order(org, status="ISSUED", issued_by=people.mm_mad)
        make_order(org, status="CLOSED", issued_by=people.mm_mad, expected_date=today)
        make_order(org)
        waiting = panels.awaiting_receipt(ctx)
        assert waiting.count == 3
        assert [row.label.split(" · ")[0] for row in waiting.rows] == [
            late.number,
            soon.number,
            undated.number,
        ]
        assert waiting.rows[2].detail == "no date"
        overdue = panels.overdue(ctx)
        assert overdue.count == 1 and overdue.rows[0].label.startswith(late.number or "")
        assert panels.awaiting_receipt(other).count == 0
        assert panels.overdue(other).count == 0

    def test_dispatches_needing_attention(
        self, org: Org, people: People, ctx: AccessContext, other: AccessContext
    ) -> None:
        po = make_order(org, status="ISSUED", issued_by=people.mm_mad)
        make_dispatch(po, Status.AMBIGUOUS)
        make_dispatch(po, Status.QUARANTINED)
        make_dispatch(po, Status.QUEUED, kind=Kind.PO_CANCELLATION)
        make_dispatch(po, Status.SENT)
        make_dispatch(po, Status.QUEUED)  # an order on its way: nothing to do
        content = panels.dispatches(ctx)
        assert content.count == 3
        assert {row.detail for row in content.rows} == {"Ambiguous", "Quarantined", "Queued"}
        assert panels.dispatches(other).count == 0


class TestGoods:
    def test_to_hand_over_is_what_was_received_and_not_given(
        self,
        org: Org,
        people: People,
        make_line: MakeLine,
        buy: Buy,
        receive: Receive,
    ) -> None:
        line = make_line("10")
        receive(buy((line, "10")), "6")
        wo = AccessContext.for_user(people.wo_mad)
        content = panels.to_hand_over(wo)
        assert content.count == 1
        assert content.rows[0].detail == "6 M"
        assert content.rows[0].href.endswith(f"?request={line.request_id}")
        assert panels.to_hand_over(AccessContext.for_user(people.mm_sev)).count == 0

    def test_to_hand_over_stops_at_the_cap_and_unallocated_too(
        self,
        monkeypatch: pytest.MonkeyPatch,
        people: People,
        make_line: MakeLine,
        buy: Buy,
        receive: Receive,
    ) -> None:
        monkeypatch.setattr(panels, "COUNT_CAP", 1)
        for _ in range(2):
            receive(buy((make_line("10"), "10")), "6")
        wo = AccessContext.for_user(people.wo_mad)
        content = panels.to_hand_over(wo)
        assert (content.count, content.more, content.label) == (1, True, "1+")
        assert panels.unallocated(AccessContext.for_user(people.mm_mad)).more is False

    def test_the_stretches_of_awaiting_handover_give_the_whole_list(
        self,
        people: People,
        make_line: MakeLine,
        buy: Buy,
        receive: Receive,
    ) -> None:
        for _ in range(3):
            receive(buy((make_line("10"), "10")), "6")
        make_line("10")  # nothing received: not awaiting
        wo = AccessContext.for_user(people.wo_mad)
        eager = handover_selectors.awaiting_handover(wo)
        awaiting = handover_selectors.AwaitingHandover(wo)
        for size in (1, 2, 50):
            lazy = read_all(awaiting.seek, awaiting.window, size)
            assert [(e.line.pk, e.awaiting_handover) for e in lazy] == [
                (e.line.pk, e.awaiting_handover) for e in eager
            ]
        assert len(eager) == 3
        assert (
            every_key(
                handover_selectors.AwaitingHandover(AccessContext.for_user(people.mm_sev)).seek
            )
            == []
        )

    def test_site_surplus_is_what_a_worksite_could_not_take(
        self,
        org: Org,
        people: People,
        ctx: AccessContext,
        other: AccessContext,
        make_line: MakeLine,
        buy: Buy,
        receive: Receive,
    ) -> None:
        warehouse_line = make_line("10")
        receive(buy((warehouse_line, "10")), "10")  # a warehouse leaves no surplus
        assert panels.site_surplus(ctx).count == 0
        x, y = make_line("10", project=org.hospital), make_line("10", project=org.sev_for_mad)
        receive(buy((x, "10"), (y, "10")), "20", at=org.worksite(org.hospital))
        content = panels.site_surplus(ctx)
        assert content.count == 1
        assert content.rows[0].detail == "10 M"
        assert panels.site_surplus(other).count == 0

    def test_closed_requests_with_a_line_closed_short(
        self, ctx: AccessContext, other: AccessContext, make_line: MakeLine, people: People
    ) -> None:
        plain = make_line("4", status=MaterialRequest.Status.CLOSED)
        short = make_line("4", status=MaterialRequest.Status.CLOSED)
        MaterialRequestLine.objects.filter(pk=short.pk).update(
            closed_short_at=timezone.now(),
            closed_short_by=people.mm_mad,
            closed_short_source="LINE",
            close_reason="supplier stopped the item",
        )
        old = make_line("4", status=MaterialRequest.Status.CLOSED)
        MaterialRequestLine.objects.filter(pk=old.pk).update(
            closed_short_at=timezone.now(),
            closed_short_by=people.mm_mad,
            closed_short_source="LINE",
            close_reason="too old",
        )
        MaterialRequest.objects.filter(pk=old.request_id).update(
            closed_at=timezone.now() - timedelta(days=40)
        )
        content = panels.closed_attention(ctx)
        assert content.count == 1
        assert content.rows[0].label == short.request.number
        assert plain.request.number not in {row.label for row in content.rows}
        assert panels.closed_attention(other).count == 0


class TestOverview:
    def test_counts_per_branch_for_group_administrators_only(
        self, org: Org, people: People, make_line: MakeLine
    ) -> None:
        make_line("4")
        make_order(org, status="ISSUED", issued_by=people.mm_mad)
        content = panels.overview(AccessContext.for_user(people.group_admin))
        by_label = {row.label: row.detail for row in content.rows}
        assert content.count == len(content.rows) >= 3
        assert "1 open requests" in by_label["A · A Madrid"] + by_label.get("B · B Madrid", "")
        assert "1 orders" in by_label["A · A Madrid"]
        assert panels.overview(AccessContext.for_user(people.mm_mad)).count == 0

    def test_the_list_is_limited_but_the_count_is_not(self, org: Org, people: People) -> None:
        many = panels._content(9, [panels.Row(str(n)) for n in range(9)])
        assert many.count == 9 and len(many.rows) == panels.LIMIT

"""The dashboard's charts and their aggregate selectors (plan M8b.5): counts on a known
dataset, empty weeks filled from the calendar, each count within the scope of its capability
(a request of another branch never moves a chart; a partner's counts are their own), and never
a price or a quantity."""

from collections.abc import Iterator
from datetime import datetime, timedelta
from typing import Any

import pytest
from django.utils import timezone, translation

from apps.access.context import AccessContext
from apps.access.principals import SessionPrincipal
from apps.access.roles import Role
from apps.dashboards import charts
from apps.dashboards.chart import BAR, CATEGORY, HBAR, METERS, WEEK, Chart, Series
from apps.dashboards.period import Period
from apps.handovers import selectors as handover_selectors
from apps.material_requests import selectors as request_selectors
from apps.material_requests.models import MaterialRequest, MaterialRequestLine
from apps.organizations import selectors as org_selectors
from apps.purchasing import selectors as purchasing_selectors
from apps.purchasing.models import PurchaseOrder
from apps.receiving import selectors as receiving_selectors
from apps.receiving import services as receiving_services
from apps.suppliers.models import Supplier
from tests.org import Org, People, Sentinel
from tests.support import grant
from tests.unit.dashboards.conftest import Requests
from tests.unit.handovers.conftest import HandOver
from tests.unit.purchasing.conftest import Buy, MakeLine
from tests.unit.receiving.conftest import Receive

pytestmark = pytest.mark.django_db

Status = MaterialRequest.Status
Decision = MaterialRequestLine.Decision
HOUR, DAY = timedelta(hours=1), timedelta(days=1)


@pytest.fixture(autouse=True)
def english() -> Iterator[None]:
    with translation.override("en"):
        yield


@pytest.fixture
def period() -> Period:
    return Period(12, timezone.now())


def ctx_of(user: Any) -> AccessContext:
    return AccessContext.for_user(user)


# --- requests ---------------------------------------------------------------------------


class TestOwnRequests:
    def test_own_counts_by_status_and_recent_closures(
        self, people: People, requests: Requests
    ) -> None:
        now = timezone.now()
        requests.make(status=Status.DRAFT)
        requests.make(status=Status.SUBMITTED)
        requests.make(status=Status.APPROVED, decided=now)
        requests.make(status=Status.CLOSED, decided=now, closed=now - 10 * DAY)
        requests.make(status=Status.CLOSED, decided=now, closed=now - 40 * DAY)
        requests.make(status=Status.SUBMITTED, by=people.partner)  # not the technician's
        counts = request_selectors.own_counts(ctx_of(people.tech_mad), now=now)
        assert (counts.drafts, counts.submitted, counts.approved, counts.closed) == (1, 1, 1, 1)
        partner = request_selectors.own_counts(ctx_of(people.partner), now=now)
        assert (partner.drafts, partner.submitted, partner.approved) == (0, 1, 0)

    def test_progress_counts_delivered_lines_of_live_approved_ones(
        self,
        people: People,
        period: Period,
        make_line: MakeLine,
        buy: Buy,
        receive: Receive,
        hand_over: HandOver,
    ) -> None:
        done, open_ = make_line("4"), make_line("4")
        receive(buy((done, "4"), (open_, "4")), "8")
        hand_over((done, "4"))
        chart = charts.request_progress(ctx_of(people.tech_mad), period)
        assert chart.kind == METERS
        assert {(m.label, m.value, m.maximum) for m in chart.meters} == {
            (done.request.number, 1, 1),
            (open_.request.number, 0, 1),
        }
        assert all(m.href.startswith("/requests/") for m in chart.meters)
        assert charts.request_progress(ctx_of(people.partner), period).empty


class TestReviewCharts:
    def test_inbox_by_age_in_review_scope(
        self, org: Org, people: People, period: Period, requests: Requests
    ) -> None:
        now = timezone.now()
        for age in (2 * HOUR, 2 * DAY, 5 * DAY, 9 * DAY, 10 * DAY):
            requests.make(submitted=now - age)
        requests.make(project=org.sev_works, submitted=now - HOUR)  # A-SEV's to review
        chart = charts.inbox_age(ctx_of(people.mm_mad), period)
        assert chart.series[0].values == (1, 1, 1, 2)
        assert chart.summary == "Requests waiting for a decision: 5; more than 7 days: 2."
        assert chart.hint == "5 waiting"
        assert charts.inbox_age(ctx_of(people.mm_sev), period).series[0].values == (1, 0, 0, 0)

    def test_decisions_per_week_fill_the_empty_weeks(
        self, org: Org, people: People, period: Period, requests: Requests
    ) -> None:
        this_week = period.calendar.starts[-1]
        decided = timezone.make_aware(datetime.combine(this_week, datetime.min.time())) + HOUR
        requests.make(
            status=Status.APPROVED,
            decided=decided,
            lines=(Decision.APPROVED, Decision.APPROVED, Decision.REJECTED),
        )
        requests.make(
            project=org.sev_works,
            status=Status.APPROVED,
            decided=decided,
            lines=(Decision.APPROVED,),
        )  # another branch's decision
        requests.make(status=Status.APPROVED, decided=decided - 200 * DAY, lines=("APPROVED",))
        chart = charts.decisions(ctx_of(people.mm_mad), period)
        approved, rejected = chart.series
        assert len(chart.labels) == 12 and chart.axis == WEEK and chart.stacked
        assert approved.values == (0,) * 11 + (2,)
        assert rejected.values == (0,) * 11 + (1,)
        assert chart.summary == "Lines decided in the last 12 weeks: 2 approved, 1 rejected."
        assert chart.payload["labels"][-1] == this_week.isoformat()
        assert chart.rows[-1][1] == [2, 1]
        assert charts.decisions(ctx_of(people.mm_sev), period).series[0].total == 1

    def test_the_median_time_to_a_decision_and_its_weeks(
        self, people: People, period: Period, requests: Requests
    ) -> None:
        now = timezone.now()
        for hours in (2, 4, 30):
            requests.make(
                status=Status.APPROVED, submitted=now - DAY - hours * HOUR, decided=now - DAY
            )
        ctx = ctx_of(people.mm_mad)
        median = request_selectors.median_decision_time(ctx, since=period.since, until=now)
        assert median == 4 * HOUR
        before = request_selectors.median_decision_time(
            ctx, since=period.before, until=period.since
        )
        assert before is None
        weekly = request_selectors.median_by_week(ctx, since=period.since)
        assert list(weekly.values()) == [4 * HOUR]


# --- orders and goods ----------------------------------------------------------------------


def _past_due(order: PurchaseOrder, days: int = 3) -> None:
    PurchaseOrder.objects.filter(pk=order.pk).update(
        expected_date=timezone.localdate() - timedelta(days=days)
    )


class TestOrderCharts:
    def test_orders_issued_and_receipts_posted_per_week(
        self,
        people: People,
        mm: SessionPrincipal,
        period: Period,
        make_line: MakeLine,
        buy: Buy,
        receive: Receive,
    ) -> None:
        first = buy((make_line("10"), "10"))
        buy((make_line("10"), "10"))
        buy((make_line("10"), "10"), issue=False)  # a draft was never issued
        receive(first, "4")
        void = receive(first, "2")
        receiving_services.void_receipt(mm, void.pk, reason="typed twice")  # not counted
        chart = charts.orders_and_receipts(ctx_of(people.wo_mad), period)
        orders, receipts = chart.series
        assert (orders.total, receipts.total) == (2, 1)
        assert orders.values[-1] == 2
        assert chart.summary == "In the last 12 weeks: 2 orders issued, 1 receipts posted."
        assert charts.orders_and_receipts(ctx_of(people.mm_sev), period).empty

    def test_on_time_lines_by_the_expected_date(
        self,
        people: People,
        period: Period,
        make_line: MakeLine,
        buy: Buy,
        receive: Receive,
    ) -> None:
        late, early, undated = (buy((make_line("10"), "10")) for _ in range(3))
        _past_due(late)
        PurchaseOrder.objects.filter(pk=early.pk).update(expected_date=timezone.localdate() + DAY)
        PurchaseOrder.objects.filter(pk=undated.pk).update(expected_date=None)
        for order in (late, early, undated):
            receive(order, "5")
        chart = charts.on_time(ctx_of(people.mm_mad), period)
        on_time, behind = chart.series
        assert (on_time.total, behind.total) == (1, 1)
        assert chart.hint == "50% on time"
        found = receiving_selectors.on_time_by_week(ctx_of(people.mm_sev), since=period.since)
        assert found == {}

    def test_on_time_without_lines_is_empty(self, people: People, period: Period) -> None:
        chart = charts.on_time(ctx_of(people.mm_mad), period)
        assert chart.empty and chart.hint == "0% on time"

    def test_overdue_by_supplier_ranks_and_counts_the_rest(
        self,
        monkeypatch: pytest.MonkeyPatch,
        people: People,
        period: Period,
        make_line: MakeLine,
        buy: Buy,
    ) -> None:
        other = Supplier.objects.create(name="Second Supplies", tax_id="B11111111")
        for _ in range(2):
            _past_due(buy((make_line("10"), "10")))
        third = buy((make_line("10"), "10"))
        PurchaseOrder.objects.filter(pk=third.pk).update(supplier=other)
        _past_due(third)
        buy((make_line("10"), "10"))  # not late
        monkeypatch.setattr(charts, "TOP", 1)
        chart = charts.overdue_by_supplier(ctx_of(people.wo_mad), period)
        assert chart.kind == HBAR and chart.axis == CATEGORY
        assert chart.labels == (Sentinel.SUPPLIER_NAME.value,)
        assert chart.series[0].values == (2,)
        assert chart.hint == "3 overdue, 1 with other suppliers"
        summary = purchasing_selectors.overdue_summary(
            ctx_of(people.wo_mad), today=timezone.localdate()
        )
        assert summary.count == 3
        assert summary.oldest == timezone.localdate() - timedelta(days=3)

    def test_no_overdue_order_is_an_empty_ranking(self, people: People, period: Period) -> None:
        chart = charts.overdue_by_supplier(ctx_of(people.wo_mad), period)
        assert chart.empty and chart.hint == "0 overdue"
        assert chart.summary == "Overdue orders: 0; the most with : 0."


class TestGoodsCharts:
    def test_receipts_and_handovers_per_week(
        self,
        org: Org,
        people: People,
        period: Period,
        make_line: MakeLine,
        buy: Buy,
        receive: Receive,
        hand_over: HandOver,
    ) -> None:
        line = make_line("10")
        receive(buy((line, "10")), "6")
        hand_over((line, "4"))
        chart = charts.receipts_and_handovers(ctx_of(people.site_manager), period)
        receipts, handovers = chart.series
        assert chart.kind == BAR and not chart.stacked
        # The site manager reads the hospital's handovers but not a warehouse's receipts.
        assert (receipts.total, handovers.total) == (0, 1)
        whole = charts.receipts_and_handovers(ctx_of(people.wo_mad), period)
        assert (whole.series[0].total, whole.series[1].total) == (1, 1)
        assert handover_selectors.posted_by_week(ctx_of(people.mm_sev), since=period.since) == {}

    def test_awaiting_handover_by_project_matches_the_ledger(
        self,
        org: Org,
        people: People,
        period: Period,
        make_line: MakeLine,
        buy: Buy,
        receive: Receive,
        hand_over: HandOver,
    ) -> None:
        given = make_line("10")
        received = [make_line("10") for _ in range(2)]
        other = make_line("10", project=org.sev_for_mad)
        receive(buy((given, "10"), *((line, "10") for line in received), (other, "10")), "40")
        hand_over((given, "10"))  # all of it handed over: nothing awaits
        make_line("10")  # nothing received
        ctx = ctx_of(people.wo_mad)
        pairs, rest = handover_selectors.awaiting_by_project(ctx, limit=8)
        eager = handover_selectors.awaiting_handover(ctx)  # within handover.post scope
        by_project: dict[str, set[Any]] = {}
        for entry in eager:
            by_project.setdefault(entry.line.project.name, set()).add(entry.line.request_id)
        assert pairs == sorted(
            ((name, len(ids)) for name, ids in by_project.items()), key=lambda p: (-p[1], p[0])
        )
        assert rest == 0
        assert dict(pairs) == {"HOSP-X": 2, "SEV-2": 1}
        top, rest = handover_selectors.awaiting_by_project(ctx, limit=1)
        assert (top, rest) == ([("HOSP-X", 2)], 1)
        chart = charts.awaiting_handover(ctx, period)
        assert chart.labels == ("HOSP-X", "SEV-2")
        assert chart.hint == "by project"
        # The hospital's site manager sees the hospital's only.
        mine = charts.awaiting_handover(ctx_of(people.site_manager), period)
        assert mine.labels == ("HOSP-X",)
        assert charts.awaiting_handover(ctx_of(people.mm_sev), period).empty

    def test_a_partial_handover_and_a_ranking_with_a_rest(
        self,
        monkeypatch: pytest.MonkeyPatch,
        org: Org,
        people: People,
        make_line: MakeLine,
        buy: Buy,
        receive: Receive,
        hand_over: HandOver,
    ) -> None:
        partly = make_line("10")
        other = make_line("10", project=org.sev_for_mad)
        receive(buy((partly, "10"), (other, "10")), "20")
        hand_over((partly, "4"))  # 6 still waits
        monkeypatch.setattr(charts, "TOP", 1)
        chart = charts.awaiting_handover(ctx_of(people.mm_mad), Period(12, timezone.now()))
        assert chart.series[0].values == (1,)
        assert chart.hint == "2 in all, 1 for other projects"


# --- organization ----------------------------------------------------------------------


class TestOrganizationCharts:
    def test_requests_submitted_per_branch_stack_by_week(
        self,
        monkeypatch: pytest.MonkeyPatch,
        org: Org,
        people: People,
        period: Period,
        requests: Requests,
    ) -> None:
        requests.make()  # bought by A-MAD
        requests.make()
        requests.make(project=org.sev_works)  # A-SEV
        requests.make(status=Status.DRAFT)  # never submitted
        ctx = ctx_of(people.group_admin)
        chart = charts.branch_requests(ctx, period)
        assert [(s.label, s.total) for s in chart.series] == [
            ("A · A Madrid", 2),
            ("A · A Sevilla", 1),
        ]
        assert chart.summary == ("Requests submitted in the last 12 weeks: 3, from 2 branches.")
        monkeypatch.setattr(charts, "STACKED_BRANCHES", 1)
        folded = charts.branch_requests(ctx, period)
        assert [(s.label, s.total) for s in folded.series] == [
            ("A · A Madrid", 2),
            ("Other branches", 1),
        ]

    def test_people_per_branch_count_valid_roles_of_active_accounts(
        self, monkeypatch: pytest.MonkeyPatch, org: Org, people: People, period: Period
    ) -> None:
        ctx = ctx_of(people.group_admin)
        found = {
            (branch.company.code, branch.code): n
            for branch, n in org_selectors.people_per_branch(ctx)
        }
        mad, sev = found[("A", "MAD")], found[("A", "SEV")]
        grant(people.nobody, Role.TECHNICIAN, branch=org.a_sev)
        again = {
            (branch.company.code, branch.code): n
            for branch, n in org_selectors.people_per_branch(ctx)
        }
        assert again[("A", "MAD")] == mad
        assert again[("A", "SEV")] == sev + 1
        assert found[("C", "HQ")] == 0  # the partner's role is company-wide
        chart = charts.branch_people(ctx, period)
        assert chart.kind == HBAR and chart.hint == "valid today"
        monkeypatch.setattr(charts, "TOP", 1)
        assert "in other branches" in charts.branch_people(ctx, period).hint

    def test_none_of_it_for_anyone_else(self, people: People) -> None:
        ctx = ctx_of(people.mm_mad)
        assert org_selectors.people_per_branch(ctx) == []
        assert org_selectors.branch_labels(ctx, set()) == {}


# --- the chart model ---------------------------------------------------------------------


def test_a_chart_without_counts_is_empty_and_its_rows_follow_the_labels() -> None:
    chart = Chart(BAR, labels=("a", "b"), series=(Series("x", (0, 0)), Series("y", (0, 0))))
    assert chart.empty
    assert chart.rows == [("a", [0, 0]), ("b", [0, 0])]
    assert chart.payload == {
        "type": BAR,
        "axis": CATEGORY,
        "stacked": False,
        "labels": ["a", "b"],
        "series": [{"label": "x", "data": [0, 0]}, {"label": "y", "data": [0, 0]}],
    }
    assert '"type":"bar"' in chart.payload_json
    assert not Chart(METERS).meters and Chart(METERS).empty

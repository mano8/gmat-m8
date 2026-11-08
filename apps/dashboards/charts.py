"""The dashboard's chart panels (plan M8b.5): counts and durations only.

Each panel reads through the owning app's aggregate selectors, every set of documents
through ``visible_to`` of the capability that governs it (S6); a panel that reads more than
one kind of document ``requires`` each capability, so nobody sees a count of documents
they could not open. Never a price (spend charts come with the inventory plan, under
``price.view``), a supplier only with ``supplier.view``, a requester's own requests only.
"""

from collections.abc import Iterable, Sequence
from datetime import date
from typing import Any

from django.urls import reverse
from django.utils.translation import gettext as _
from django.utils.translation import gettext_lazy

from apps.access.context import AccessContext
from apps.access.roles import Capability
from apps.core.stats import top
from apps.dashboards.chart import (
    BAR,
    HBAR,
    LINE,
    METERS,
    Chart,
    ChartPanel,
    Meter,
    Series,
    weekly_chart,
)
from apps.dashboards.period import Period
from apps.handovers import selectors as handover_selectors
from apps.material_requests import selectors as request_selectors
from apps.organizations import selectors as org_selectors
from apps.purchasing import selectors as purchasing_selectors
from apps.receiving import selectors as receiving_selectors

TOP = 8  # bars of a ranking (projects, suppliers, branches); the rest is one more bar
STACKED_BRANCHES = 5  # series of the branches chart; the rest is one more series
PROGRESS_REQUESTS = 5
Decision = request_selectors.Decision


def _split(found: dict[tuple[date, Any], int], key: object) -> dict[date, int]:
    return {week: total for (week, value), total in found.items() if value == key}


def _ranked(pairs: Sequence[tuple[str, int]], *, series: str, **more: Any) -> Chart:
    """A ranking as horizontal bars: the first ``TOP`` only (the others would dwarf them;
    the summary and the hint say how many they hold)."""
    labels = tuple(label for label, _ in pairs)
    counts = tuple(count for _, count in pairs)
    return Chart(HBAR, labels=labels, series=(Series(series, counts),), **more)


# --- requests ---------------------------------------------------------------------------


def request_progress(ctx: AccessContext, period: Period) -> Chart:
    """The user's latest approved requests: lines delivered of lines approved."""
    meters = tuple(
        Meter(
            found.request.number or "",
            found.request.project.code,
            found.delivered_lines,
            found.approved_lines,
            reverse("requests:detail", args=[found.request.pk]),
        )
        for found in request_selectors.recent_progress(ctx, limit=PROGRESS_REQUESTS)
    )
    return Chart(METERS, meters=meters, hint=_("lines delivered of lines approved"))


def inbox_age(ctx: AccessContext, period: Period) -> Chart:
    ages = request_selectors.inbox_ages(ctx, now=period.now)
    labels = (_("Under 1 day"), _("1 to 3 days"), _("3 to 7 days"), _("Over 7 days"))
    return Chart(
        BAR,
        labels=labels,
        series=(Series(_("Requests"), ages),),
        summary=_("Requests waiting for a decision: %(total)d; more than 7 days: %(old)d.")
        % {"total": sum(ages), "old": ages[-1]},
        hint=_("%(total)d waiting") % {"total": sum(ages)},
        label_head=_("Waiting"),
    )


def decisions(ctx: AccessContext, period: Period) -> Chart:
    found = request_selectors.decisions_by_week(ctx, since=period.since)
    fill = period.calendar.fill
    approved = Series(_("Approved"), tuple(fill(_split(found, Decision.APPROVED))))
    rejected = Series(_("Rejected"), tuple(fill(_split(found, Decision.REJECTED))))
    return weekly_chart(
        BAR,
        period.calendar,
        (approved, rejected),
        stacked=True,
        summary=_(
            "Lines decided in the last %(weeks)d weeks: %(approved)d approved, "
            "%(rejected)d rejected."
        )
        % {"weeks": period.weeks, "approved": approved.total, "rejected": rejected.total},
        hint=_("lines approved and rejected"),
        label_head=_("Week of"),
    )


# --- orders and receipts -------------------------------------------------------------------


def orders_and_receipts(ctx: AccessContext, period: Period) -> Chart:
    fill = period.calendar.fill
    orders = Series(
        _("Orders issued"),
        tuple(fill(purchasing_selectors.issued_by_week(ctx, since=period.since))),
    )
    receipts = Series(
        _("Receipts posted"),
        tuple(fill(receiving_selectors.posted_by_week(ctx, since=period.since))),
    )
    return weekly_chart(
        LINE,
        period.calendar,
        (orders, receipts),
        summary=_(
            "In the last %(weeks)d weeks: %(orders)d orders issued, %(receipts)d receipts posted."
        )
        % {"weeks": period.weeks, "orders": orders.total, "receipts": receipts.total},
        hint=_("per week"),
        label_head=_("Week of"),
    )


def on_time(ctx: AccessContext, period: Period) -> Chart:
    found = receiving_selectors.on_time_by_week(ctx, since=period.since)
    fill = period.calendar.fill
    early = Series(_("By the expected date"), tuple(fill(_split(found, True))))
    late = Series(_("Late"), tuple(fill(_split(found, False))))
    lines = early.total + late.total
    share = round(100 * early.total / lines) if lines else 0
    return weekly_chart(
        BAR,
        period.calendar,
        (early, late),
        stacked=True,
        summary=_(
            "Receipt lines of the last %(weeks)d weeks that arrived by the expected date: "
            "%(on_time)d of %(lines)d (%(share)d%%)."
        )
        % {"weeks": period.weeks, "on_time": early.total, "lines": lines, "share": share},
        hint=_("%(share)d%% on time") % {"share": share},
        label_head=_("Week of"),
    )


def overdue_by_supplier(ctx: AccessContext, period: Period) -> Chart:
    pairs, rest = purchasing_selectors.overdue_by_supplier(ctx, today=period.today, limit=TOP)
    count = sum(total for _, total in pairs) + rest
    name, most = pairs[0] if pairs else ("", 0)
    return _ranked(
        pairs,
        series=_("Overdue orders"),
        summary=_("Overdue orders: %(count)d; the most with %(name)s: %(top)d.")
        % {"count": count, "name": name, "top": most},
        hint=(
            _("%(count)d overdue, %(rest)d with other suppliers")
            if rest
            else _("%(count)d overdue")
        )
        % {"count": count, "rest": rest},
        label_head=_("Supplier"),
    )


# --- goods ---------------------------------------------------------------------------------


def receipts_and_handovers(ctx: AccessContext, period: Period) -> Chart:
    fill = period.calendar.fill
    receipts = Series(
        _("Receipts"), tuple(fill(receiving_selectors.posted_by_week(ctx, since=period.since)))
    )
    handovers = Series(
        _("Handovers"), tuple(fill(handover_selectors.posted_by_week(ctx, since=period.since)))
    )
    return weekly_chart(
        BAR,
        period.calendar,
        (receipts, handovers),
        summary=_(
            "Posted in the last %(weeks)d weeks: %(receipts)d receipts, %(handovers)d handovers."
        )
        % {"weeks": period.weeks, "receipts": receipts.total, "handovers": handovers.total},
        hint=_("posted per week"),
        label_head=_("Week of"),
    )


def awaiting_handover(ctx: AccessContext, period: Period) -> Chart:
    pairs, rest = handover_selectors.awaiting_by_project(ctx, limit=TOP)
    count = sum(total for _, total in pairs) + rest
    name, most = pairs[0] if pairs else ("", 0)
    return _ranked(
        pairs,
        series=_("Requests"),
        summary=_("Requests awaiting handover: %(count)d; the most for %(name)s: %(top)d.")
        % {"count": count, "name": name, "top": most},
        hint=(_("%(count)d in all, %(rest)d for other projects") if rest else _("by project"))
        % {"count": count, "rest": rest},
        label_head=_("Project"),
    )


# --- organization --------------------------------------------------------------------------


def _branch_series(
    period: Period, found: dict[tuple[date, Any], int], labels: dict[Any, str]
) -> list[Series]:
    totals: dict[Any, int] = {}
    for (_week, branch), total in found.items():
        totals[branch] = totals.get(branch, 0) + total
    shown, _rest = top(totals.items(), STACKED_BRANCHES)
    kept = [branch for branch, _total in shown]
    fill = period.calendar.fill
    series = [
        Series(labels.get(branch, "?"), tuple(fill(_split(found, branch)))) for branch in kept
    ]
    others: dict[date, int] = {}
    for (week, branch), total in found.items():
        if branch not in kept:
            others[week] = others.get(week, 0) + total
    if others:
        series.append(Series(_("Other branches"), tuple(fill(others))))
    return series


def branch_requests(ctx: AccessContext, period: Period) -> Chart:
    found = request_selectors.submitted_by_branch(ctx, since=period.since)
    labels = org_selectors.branch_labels(ctx, {branch for _week, branch in found})
    series = _branch_series(period, found, labels)
    count = sum(each.total for each in series)
    return weekly_chart(
        BAR,
        period.calendar,
        series,
        stacked=True,
        summary=_(
            "Requests submitted in the last %(weeks)d weeks: %(count)d, from %(branches)d branches."
        )
        % {"weeks": period.weeks, "count": count, "branches": len(labels)},
        hint=_("by procurement branch"),
        label_head=_("Week of"),
    )


def _branch_people(rows: Iterable[tuple[Any, int]]) -> list[tuple[str, int]]:
    return [(f"{branch.company.code} · {branch.name}", people) for branch, people in rows]


def branch_people(ctx: AccessContext, period: Period) -> Chart:
    rows = _branch_people(org_selectors.people_per_branch(ctx))
    pairs, rest = top(rows, TOP)
    count = sum(people for _, people in rows)
    return _ranked(
        pairs,
        series=_("People"),
        summary=_("People with a valid role: %(count)d, in %(branches)d branches.")
        % {"count": count, "branches": len(rows)},
        hint=(_("valid today, %(rest)d in other branches") if rest else _("valid today"))
        % {"rest": rest},
        label_head=_("Branch"),
    )


# --- the registry ----------------------------------------------------------------------------

C = Capability
CHARTS: tuple[ChartPanel, ...] = (
    ChartPanel(
        "decisions",
        gettext_lazy("Decisions per week"),
        C.REQUEST_REVIEW,
        decisions,
        "requests:inbox",
        gettext_lazy("No line was decided in these weeks."),
    ),
    ChartPanel(
        "inbox-age",
        gettext_lazy("Inbox by age"),
        C.REQUEST_REVIEW,
        inbox_age,
        "requests:inbox",
        gettext_lazy("Nothing is waiting for a decision."),
    ),
    ChartPanel(
        "orders-receipts",
        gettext_lazy("Orders issued and receipts posted"),
        C.PO_VIEW,
        orders_and_receipts,
        "purchasing:orders",
        gettext_lazy("No order was issued and no receipt posted in these weeks."),
        requires=(C.RECEIPT_VIEW,),
    ),
    ChartPanel(
        "on-time",
        gettext_lazy("On-time receipts"),
        C.PO_VIEW,
        on_time,
        "receiving:list",
        gettext_lazy("No receipt of an order with an expected date in these weeks."),
        requires=(C.RECEIPT_VIEW,),
    ),
    ChartPanel(
        "overdue-suppliers",
        gettext_lazy("Overdue orders by supplier"),
        C.PO_VIEW,
        overdue_by_supplier,
        "purchasing:orders",
        gettext_lazy("No order is past its expected date."),
        requires=(C.SUPPLIER_VIEW,),
        query="?status=ISSUED",
    ),
    ChartPanel(
        "receipts-handovers",
        gettext_lazy("Receipts and handovers"),
        C.RECEIPT_VIEW,
        receipts_and_handovers,
        "receiving:list",
        gettext_lazy("No receipt or handover was posted in these weeks."),
        requires=(C.HANDOVER_VIEW,),
    ),
    ChartPanel(
        "handover-projects",
        gettext_lazy("Awaiting handover"),
        C.HANDOVER_POST,  # who hands over, like the "to hand over" list panel (plan §3.4)
        awaiting_handover,
        "handovers:list",
        gettext_lazy("Nothing received is waiting to be handed over."),
        requires=(C.HANDOVER_VIEW,),
    ),
    ChartPanel(
        "branch-requests",
        gettext_lazy("Requests submitted per branch"),
        C.ORG_MANAGE,
        branch_requests,
        "requests:inbox",
        gettext_lazy("No request was submitted in these weeks."),
        requires=(C.REQUEST_VIEW,),
        wide=True,
    ),
    ChartPanel(
        "branch-people",
        gettext_lazy("People per branch"),
        C.ORG_MANAGE,
        branch_people,
        "org:people",
        gettext_lazy("No branch has anyone with a valid role."),
    ),
    ChartPanel(
        "request-progress",
        gettext_lazy("Approved and delivered"),
        C.REQUEST_CREATE,
        request_progress,
        "requests:list",
        gettext_lazy("None of your requests is approved yet."),
        requires=(C.REQUEST_VIEW,),
        kind="meters",
    ),
)

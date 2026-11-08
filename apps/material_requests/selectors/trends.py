"""Counts and durations of requests for the dashboard's tiles and charts (plan M8b.5).

Everything is counted in the database through ``visible_to`` of one capability (S6: never a
mix of memberships): the user's own requests through ``request.view`` (``my_requests``), the
inbox and the decisions through ``request.review``. No quantity of another project and no
price is read here; only the requester's own progress reads the ledger, line by line.
"""

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any

from django.db.models import Count, Q, QuerySet

from apps.access.context import AccessContext
from apps.core.stats import median_duration, weekly_aggregate, weekly_by
from apps.material_requests import ledger
from apps.material_requests.models import MaterialRequest, MaterialRequestLine
from apps.material_requests.selectors.requester import VIEW, Decision, Status, my_requests
from apps.material_requests.selectors.reviewer import REVIEW

RECENTLY_CLOSED = timedelta(days=30)
#: The inbox's age buckets: under 1 day, 1 to 3 days, 3 to 7 days, over 7 days.
AGE_LIMITS = (timedelta(days=1), timedelta(days=3), timedelta(days=7))
DECIDED = (Decision.APPROVED, Decision.REJECTED)
PROGRESS_STATUSES = (Status.APPROVED, Status.CLOSED)


@dataclass(frozen=True)
class OwnCounts:
    """The user's own requests by status; ``closed`` counts the last 30 days only."""

    drafts: int
    submitted: int
    approved: int
    closed: int


@dataclass(frozen=True)
class Progress:
    """One of the user's requests: approved lines, and how many of them are delivered."""

    request: MaterialRequest
    approved_lines: int
    delivered_lines: int


def own_counts(ctx: AccessContext, *, now: datetime) -> OwnCounts:
    found = (
        my_requests(ctx)
        .order_by()
        .aggregate(
            drafts=Count("pk", filter=Q(status=Status.DRAFT)),
            submitted=Count("pk", filter=Q(status=Status.SUBMITTED)),
            approved=Count("pk", filter=Q(status=Status.APPROVED)),
            closed=Count(
                "pk", filter=Q(status=Status.CLOSED, closed_at__gte=now - RECENTLY_CLOSED)
            ),
        )
    )
    return OwnCounts(**found)


def _waiting(ctx: AccessContext) -> QuerySet[MaterialRequest]:
    """The inbox's requests (submitted, in ``request.review`` scope), without its order."""
    return MaterialRequest.objects.visible_to(ctx, REVIEW).filter(status=Status.SUBMITTED)


def inbox_ages(ctx: AccessContext, *, now: datetime) -> tuple[int, int, int, int]:
    """Submitted requests waiting under 1 day, 1 to 3, 3 to 7 and over 7 days."""
    day, three, seven = (now - limit for limit in AGE_LIMITS)
    found = (
        _waiting(ctx)
        .order_by()
        .aggregate(
            fresh=Count("pk", filter=Q(submitted_at__gt=day)),
            days=Count("pk", filter=Q(submitted_at__lte=day, submitted_at__gt=three)),
            week=Count("pk", filter=Q(submitted_at__lte=three, submitted_at__gt=seven)),
            old=Count("pk", filter=Q(submitted_at__lte=seven)),
        )
    )
    return found["fresh"], found["days"], found["week"], found["old"]


def decisions_by_week(ctx: AccessContext, *, since: datetime) -> dict[tuple[date, str], int]:
    """Lines approved and rejected per week (by the line's ``decided_at``)."""
    lines = MaterialRequestLine.objects.visible_to(ctx, REVIEW).filter(decision__in=DECIDED)
    return weekly_by(lines, "decided_at", since, "decision")


def _decided(ctx: AccessContext) -> QuerySet[MaterialRequest]:
    return MaterialRequest.objects.visible_to(ctx, REVIEW).filter(
        submitted_at__isnull=False, decided_at__isnull=False
    )


def median_decision_time(
    ctx: AccessContext, *, since: datetime, until: datetime
) -> timedelta | None:
    """The median time from submission to decision of the requests decided in
    ``[since, until)``; ``None`` when none was."""
    found = (
        _decided(ctx)
        .filter(decided_at__gte=since, decided_at__lt=until)
        .order_by()
        .aggregate(median=median_duration("submitted_at", "decided_at"))
    )
    return found["median"]  # type: ignore[no-any-return]


def median_by_week(ctx: AccessContext, *, since: datetime) -> dict[date, timedelta]:
    """The median time to a decision of the requests decided in each week (the weeks
    without a decision are absent)."""
    duration = median_duration("submitted_at", "decided_at")
    return weekly_aggregate(_decided(ctx), "decided_at", since, duration)


def submitted_by_branch(ctx: AccessContext, *, since: datetime) -> dict[tuple[date, Any], int]:
    """Requests submitted per week and procurement branch, in ``request.view`` scope."""
    rows = MaterialRequest.objects.visible_to(ctx, VIEW).filter(submitted_at__isnull=False)
    return weekly_by(rows, "submitted_at", since, "procurement_branch_id")


def recent_progress(ctx: AccessContext, *, limit: int) -> list[Progress]:
    """The user's own approved or closed requests, latest decision first, each with its
    approved lines (live: approved, a positive quantity, not closed short) and how many of
    them are fully delivered. Counts of lines, so units never mix."""
    requests = list(
        my_requests(ctx)
        .filter(status__in=PROGRESS_STATUSES, decided_at__isnull=False)
        .order_by("-decided_at", "-pk")[:limit]
    )
    lines = MaterialRequestLine.objects.filter(
        ledger.live_q(), request__in=[found.pk for found in requests]
    ).select_related("request")
    approved: dict[Any, int] = {}
    delivered: dict[Any, int] = {}
    for entry in ledger.line_ledgers(lines):
        key = entry.line.request_id
        approved[key] = approved.get(key, 0) + 1
        done = entry.flows.delivered >= entry.approved
        delivered[key] = delivered.get(key, 0) + int(done)
    return [
        Progress(found, approved.get(found.pk, 0), delivered.get(found.pk, 0)) for found in requests
    ]

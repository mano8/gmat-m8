"""Counts of orders for the dashboard's tiles and charts (plan M8b.5).

Through ``visible_to(ctx, po.view)`` only. Never a price; a supplier's name only in
``overdue_by_supplier``, which the dashboard offers to ``supplier.view`` holders alone.
"""

from dataclasses import dataclass
from datetime import date, datetime

from django.db.models import Count, Min

from apps.access.context import AccessContext
from apps.core.stats import weekly
from apps.purchasing.models import PurchaseOrder
from apps.purchasing.selectors.purchase_orders import VIEW, overdue


@dataclass(frozen=True)
class Overdue:
    """How many issued orders are past their expected date, and the earliest such date."""

    count: int
    oldest: date | None


def issued_by_week(ctx: AccessContext, *, since: datetime) -> dict[date, int]:
    """Orders issued per week (cancelled ones too: they were issued)."""
    rows = PurchaseOrder.objects.visible_to(ctx, VIEW).filter(issued_at__isnull=False)
    return weekly(rows, "issued_at", since)


def overdue_summary(ctx: AccessContext, *, today: date) -> Overdue:
    found = (
        overdue(ctx, today=today)
        .order_by()
        .aggregate(count=Count("pk"), oldest=Min("expected_date"))
    )
    return Overdue(found["count"], found["oldest"])


def overdue_by_supplier(
    ctx: AccessContext, *, today: date, limit: int
) -> tuple[list[tuple[str, int]], int]:
    """The ``limit`` suppliers with the most overdue orders (most first, then by name), and
    how many overdue orders the others hold."""
    rows = overdue(ctx, today=today).order_by()
    top = list(
        rows.values("supplier__name")
        .annotate(total=Count("pk"))
        .order_by("-total", "supplier__name")
        .values_list("supplier__name", "total")[:limit]
    )
    return top, rows.count() - sum(total for _, total in top)

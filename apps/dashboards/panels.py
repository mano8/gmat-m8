"""The role dashboards (plan M7.2): panels loaded lazily with HTMX, all through selectors.

A **panel** is a capability, a title and a ``build`` that reads through the owning apps'
selectors (so every row is in the user's scope for that capability) and returns a count plus
the first rows. The home page lists the panels the user's capabilities reach as empty
placeholders; each loads its own fragment from ``dashboards:panel``, so one slow panel
never holds the page and a user is never offered a panel they cannot open.

Roles reach their panels through capabilities, not role names: a technician or partner gets
*My requests*; a reviewer the inbox count; material managers the sourcing and purchasing
worklists; group administrators the overview per company and branch. Prices are never shown
here, and suppliers only to ``po.view`` holders (who hold ``supplier.view``).
"""

from collections.abc import Callable, Iterable
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from django.db.models import Count
from django.urls import reverse
from django.utils.formats import date_format
from django.utils.functional import Promise
from django.utils.translation import gettext as _
from django.utils.translation import gettext_lazy

from apps.access.context import AccessContext
from apps.access.roles import Capability
from apps.core.listing import head
from apps.dashboards.chart import ChartPanel
from apps.dashboards.charts import CHARTS
from apps.handovers import selectors as handover_selectors
from apps.material_requests import selectors as request_selectors
from apps.material_requests.models import MaterialRequest, MaterialRequestLine
from apps.organizations import selectors as org_selectors
from apps.projects.models import Project
from apps.purchasing import selectors as purchasing_selectors
from apps.purchasing.models import PurchaseOrder
from apps.receiving import selectors as receiving_selectors

LIMIT = 5  # rows a panel lists; the link beside it leads to the full screen
COUNT_CAP = 99  # a count worked out line by line stops here and reads "99+" (M7d.4)
Status = MaterialRequest.Status


@dataclass(frozen=True)
class Row:
    label: str
    detail: str = ""
    href: str = ""


@dataclass(frozen=True)
class Content:
    count: int
    rows: tuple[Row, ...] = ()
    more: bool = False  # the count stopped at the cap: there are more

    @property
    def label(self) -> str:
        return f"{self.count}+" if self.more else str(self.count)


@dataclass(frozen=True)
class Panel:
    key: str
    title: str | Promise
    capability: str
    build: Callable[[AccessContext], Content]
    # The screen with the whole list, and what to say when there is nothing.
    link: str
    empty: str | Promise
    # Whether a count above zero asks for action (the chip turns amber).
    alert: bool = False
    # A query string for the link ("?assigned=1": the same screen, filtered).
    query: str = ""
    requires: tuple[str, ...] = ()
    kind: str = "list"


# --- formatting ---------------------------------------------------------------------------


def _quantity(value: Decimal) -> str:
    return f"{value.normalize():f}"


def _material(line: MaterialRequestLine) -> str:
    return line.material.name if line.material else line.free_text


def _day(value: Any) -> str:
    return date_format(value, "SHORT_DATE_FORMAT")


def _content(total: int, rows: Iterable[Row], *, more: bool = False) -> Content:
    return Content(total, tuple(rows)[:LIMIT], more)


def _line_quantity(line: MaterialRequestLine, value: Decimal) -> str:
    return f"{_quantity(value)} {line.unit.code}"


def _examine(request: MaterialRequest) -> str:
    return reverse("requests:examine", args=[request.pk])


# --- requester and reviewer -----------------------------------------------------------------


def my_requests(ctx: AccessContext) -> Content:
    """The user's own requests still moving: draft, submitted or approved."""
    rows = request_selectors.my_requests(ctx).filter(
        status__in=[Status.DRAFT, Status.SUBMITTED, Status.APPROVED]
    )
    return _content(
        rows.count(),
        (
            Row(
                found.number or _("Draft"),
                f"{found.project.code} · {found.get_status_display()}",
                reverse("requests:detail", args=[found.pk]),
            )
            for found in rows[:LIMIT]
        ),
    )


def inbox(ctx: AccessContext) -> Content:
    rows = request_selectors.inbox(ctx)
    return _content(
        rows.count(),
        (
            Row(
                found.number or "",
                f"{found.project.code} · {found.get_priority_display()}",
                _examine(found),
            )
            for found in rows[:LIMIT]
        ),
    )


def assigned(ctx: AccessContext) -> Content:
    """Submitted, approved and rejected requests sent to the user (M7b.4; maintainer,
    2026-11-02). The inbox's "Assigned to me" filter lists each status."""
    rows = request_selectors.assigned_to(ctx, statuses=request_selectors.ASSIGNED_STATUSES)
    return _content(
        rows.count(),
        (
            Row(
                found.number or "",
                f"{found.project.code} · {found.get_status_display()}",
                _examine(found),
            )
            for found in rows[:LIMIT]
        ),
    )


# --- material managers: sourcing and purchasing ---------------------------------------------


def sourcing(ctx: AccessContext) -> Content:
    rows = request_selectors.sourcing_to_decide(ctx)
    return _content(
        rows.count(),
        (
            Row(
                f"{line.request.number} · {_material(line)}",
                _line_quantity(line, line.approved_qty or Decimal(0)),
                _examine(line.request),
            )
            for line in rows[:LIMIT]
        ),
    )


def to_purchase(ctx: AccessContext) -> Content:
    worklist = purchasing_selectors.Worklist(ctx)
    rows, more = head(worklist.seek, worklist.line_window, limit=COUNT_CAP)
    return _content(
        len(rows),
        (
            Row(
                f"{row.line.request.number} · {_material(row.line)}",
                _line_quantity(row.line, row.to_purchase),
                reverse("purchasing:worklist"),
            )
            for row in rows[:LIMIT]
        ),
        more=more,
    )


def stale_drafts(ctx: AccessContext) -> Content:
    rows = purchasing_selectors.stale_drafts(ctx)
    return _content(
        rows.count(),
        (
            Row(
                found.supplier.name,
                _("since %(day)s") % {"day": _day(found.created_at)},
                reverse("purchasing:detail", args=[found.pk]),
            )
            for found in rows[:LIMIT]
        ),
    )


def unallocated(ctx: AccessContext) -> Content:
    nobody = purchasing_selectors.Unallocated(ctx)
    rows, more = head(nobody.seek, nobody.window, limit=COUNT_CAP)
    return _content(
        len(rows),
        (
            Row(
                f"{row.line.po.number} · {row.line.material.name if row.line.material else ''}",
                f"{_quantity(row.unallocated)} {row.line.unit.code}",
                reverse("purchasing:detail", args=[row.line.po_id]),
            )
            for row in rows[:LIMIT]
        ),
        more=more,
    )


def _order_rows(orders: Iterable[PurchaseOrder]) -> Iterable[Row]:
    for found in orders:
        expected = _day(found.expected_date) if found.expected_date else _("no date")
        yield Row(
            f"{found.number} · {found.supplier.name}",
            expected,
            reverse("purchasing:detail", args=[found.pk]),
        )


def awaiting_receipt(ctx: AccessContext) -> Content:
    rows = purchasing_selectors.awaiting_receipt(ctx)
    return _content(rows.count(), _order_rows(rows[:LIMIT]))


def overdue(ctx: AccessContext) -> Content:
    rows = purchasing_selectors.overdue(ctx)
    return _content(rows.count(), _order_rows(rows[:LIMIT]))


def dispatches(ctx: AccessContext) -> Content:
    rows = purchasing_selectors.dispatches_needing_attention(ctx)
    return _content(
        rows.count(),
        (
            Row(
                f"{found.source_po.number if found.source_po else ''} · {found.get_kind_display()}",
                found.get_status_display(),
                reverse("purchasing:detail", args=[found.source_po_id]),
            )
            for found in rows[:LIMIT]
        ),
    )


# --- material managers and warehouse: goods ---------------------------------------------------


def to_hand_over(ctx: AccessContext) -> Content:
    awaiting = handover_selectors.AwaitingHandover(ctx)
    rows, more = head(awaiting.seek, awaiting.window, limit=COUNT_CAP)
    return _content(
        len(rows),
        (
            Row(
                f"{entry.line.request.number} · {_material(entry.line)}",
                _line_quantity(entry.line, entry.awaiting_handover),
                f"{reverse('handovers:new')}?request={entry.line.request_id}",
            )
            for entry in rows[:LIMIT]
        ),
        more=more,
    )


def site_surplus(ctx: AccessContext) -> Content:
    rows = receiving_selectors.site_surplus(ctx)
    return _content(
        rows.count(),
        (
            Row(
                f"{found.receipt.number} · {found.po_line.material.name}",
                f"{_quantity(found.surplus_qty)} {found.po_line.unit.code}",
                reverse("receiving:detail", args=[found.receipt_id]),
            )
            for found in rows[:LIMIT]
        ),
    )


def closed_attention(ctx: AccessContext) -> Content:
    rows = request_selectors.closed_needing_attention(ctx)
    return _content(
        rows.count(),
        (
            Row(
                found.number or "",
                f"{found.project.code} · {_day(found.closed_at)}",
                reverse("requests:ledger", args=[found.pk]),
            )
            for found in rows[:LIMIT]
        ),
    )


# --- group administrators -------------------------------------------------------------------


def _by_branch(rows: Any, field: str) -> dict[Any, int]:
    return {
        entry[field]: entry["total"]
        for entry in rows.order_by().values(field).annotate(total=Count("pk"))
    }


def overview(ctx: AccessContext) -> Content:
    """Per company and branch: active projects, open requests (submitted or approved) and
    issued orders still to receive. Counts only: no document, quantity or price."""
    branches = list(org_selectors.branches(ctx, Capability.ORG_MANAGE))
    projects = _by_branch(
        Project.objects.visible_to(ctx, Capability.PROJECT_VIEW).filter(
            status=Project.Status.ACTIVE
        ),
        "branch_id",
    )
    open_requests = _by_branch(
        MaterialRequest.objects.visible_to(ctx, Capability.REQUEST_VIEW).filter(
            status__in=[Status.SUBMITTED, Status.APPROVED]
        ),
        "procurement_branch_id",
    )
    orders = _by_branch(
        PurchaseOrder.objects.visible_to(ctx, Capability.PO_VIEW).filter(
            status=PurchaseOrder.Status.ISSUED
        ),
        "branch_id",
    )
    return Content(
        len(branches),
        tuple(
            Row(
                f"{branch.company.code} · {branch.name}",
                _("%(projects)d projects · %(requests)d open requests · %(orders)d orders")
                % {
                    "projects": projects.get(branch.pk, 0),
                    "requests": open_requests.get(branch.pk, 0),
                    "orders": orders.get(branch.pk, 0),
                },
            )
            for branch in branches
        ),
    )


# --- the registry ---------------------------------------------------------------------------

C = Capability
LISTS: tuple[Panel, ...] = (
    Panel(
        "my-requests",
        gettext_lazy("My requests"),
        C.REQUEST_VIEW,
        my_requests,
        "requests:list",
        gettext_lazy("You have no request in progress."),
    ),
    Panel(
        "inbox",
        gettext_lazy("Approval inbox"),
        C.REQUEST_REVIEW,
        inbox,
        "requests:inbox",
        gettext_lazy("Nothing is waiting for your decision."),
        alert=True,
    ),
    Panel(
        "assigned",
        gettext_lazy("Assigned to me"),
        # Only who can be assigned a request (group administrators, material managers and
        # engineers, the holders of ``request.edit``) is offered the panel.
        C.REQUEST_EDIT,
        assigned,
        "requests:inbox",
        gettext_lazy("No request was sent to you."),
        query="?assigned=1",
    ),
    Panel(
        "sourcing",
        gettext_lazy("Sourcing to decide"),
        C.REQUEST_SOURCE,
        sourcing,
        "requests:inbox",
        gettext_lazy("Every approved line has its sourcing."),
        alert=True,
    ),
    Panel(
        "to-purchase",
        gettext_lazy("To purchase"),
        C.PO_MANAGE,
        to_purchase,
        "purchasing:worklist",
        gettext_lazy("Nothing is waiting to be bought."),
        alert=True,
    ),
    Panel(
        "stale-drafts",
        gettext_lazy("Drafts older than 14 days"),
        C.PO_MANAGE,
        stale_drafts,
        "purchasing:orders",
        gettext_lazy("No draft order is holding demand for long."),
        alert=True,
    ),
    Panel(
        "unallocated",
        gettext_lazy("Unallocated on order"),
        C.PO_MANAGE,
        unallocated,
        "purchasing:orders",
        gettext_lazy("Every ordered quantity is allocated."),
        alert=True,
    ),
    Panel(
        "awaiting-receipt",
        gettext_lazy("Orders awaiting receipt"),
        C.PO_VIEW,
        awaiting_receipt,
        "purchasing:orders",
        gettext_lazy("No issued order is waiting for goods."),
    ),
    Panel(
        "overdue",
        gettext_lazy("Overdue orders"),
        C.PO_VIEW,
        overdue,
        "purchasing:orders",
        gettext_lazy("No order is past its expected date."),
        alert=True,
    ),
    Panel(
        "to-hand-over",
        gettext_lazy("To hand over"),
        C.HANDOVER_POST,
        to_hand_over,
        "handovers:new",
        gettext_lazy("Nothing received is waiting to be handed over."),
    ),
    Panel(
        "site-surplus",
        gettext_lazy("Surplus on site"),
        C.RECEIPT_VIEW,
        site_surplus,
        "receiving:list",
        gettext_lazy("No surplus was left on a worksite."),
        alert=True,
    ),
    Panel(
        "closed-attention",
        gettext_lazy("Closed requests to check"),
        C.REQUEST_SOURCE,
        closed_attention,
        "requests:list",
        gettext_lazy("No request was recently closed with undelivered lines."),
    ),
    Panel(
        "dispatches",
        gettext_lazy("Supplier emails to check"),
        C.PO_MANAGE,
        dispatches,
        "purchasing:orders",
        gettext_lazy("Every supplier email is on its way or delivered."),
        alert=True,
    ),
    Panel(
        "overview",
        gettext_lazy("Companies and branches"),
        C.ORG_MANAGE,
        overview,
        "org:people",
        gettext_lazy("There is no active branch."),
    ),
)
#: Every panel: the charts (M8b.5) and the task lists, each loaded from ``dashboard:panel``.
PANELS: tuple[Panel | ChartPanel, ...] = (*CHARTS, *LISTS)
BY_KEY = {panel.key: panel for panel in PANELS}


def capabilities(panel: Panel | ChartPanel) -> tuple[str, ...]:
    """Every capability the panel reads with: it is offered to who holds them all."""
    return (panel.capability, *panel.requires)


def visible(ctx: AccessContext) -> list[Panel | ChartPanel]:
    """The panels whose capabilities the user holds somewhere."""
    return [panel for panel in PANELS if all(ctx.has(each) for each in capabilities(panel))]

"""Reads of handovers (plan §2.3 point 3, §4.5 I9).

Warehouse stock is **not tracked** in phase 1: material may have been there before the
application. What a warehouse received and has not handed over yet is therefore only a
hint, shown as a warning on the handover form and never a limit:

    available = Σ accepted on posted receipts at that warehouse
              - Σ handed over from it on posted ``FROM_WAREHOUSE`` handovers

per material, never below zero. Only receipts at that warehouse count (I9); goods received
on a worksite never entered a warehouse. A line without a catalog material has no figure.
The figures are for whoever may post handovers from that warehouse (``handover.post``).

The screens (M6.3) read the rest from here, every row through ``visible_to``:

- **Handovers** (``handovers``, ``handover``, ``handover_lines``): ``handover.view``, which
  reaches the handovers of an assigned project (○), so a site manager reads them; posting
  is ``handover.post`` (``posting_projects``, ``posting_project``, ``posting_request``).
- **The handover form** (``open_lines``, ``sources``, ``receivers``): the open lines of a
  project (or of one request) with what is still to hand over (A - D), the warehouses of
  the requests' branch scope (C7, C8) and the people who may take the material (an active
  user who holds ``project.view`` on the project, as the service checks).
"""

from collections.abc import Collection, Mapping
from dataclasses import dataclass
from decimal import Decimal
from uuid import UUID

from django.db.models import Q, QuerySet, Sum

from apps.access.context import AccessContext
from apps.access.exceptions import NotInScope
from apps.access.policy import valid_now
from apps.access.roles import Capability
from apps.access.scopes import object_in_scope
from apps.accounts.models import User
from apps.catalog.models import Material
from apps.handovers.models import Handover, HandoverLine
from apps.locations.models import Location
from apps.material_requests import ledger as request_ledger
from apps.material_requests.models import MaterialRequest, MaterialRequestLine
from apps.organizations.models import Membership
from apps.projects.models import Project, ProjectAssignment
from apps.receiving.models import GoodsReceipt, GoodsReceiptLine

VIEW, POST, VOID = Capability.HANDOVER_VIEW, Capability.HANDOVER_POST, Capability.HANDOVER_VOID
ZERO = Decimal("0")


@dataclass(frozen=True)
class Shortfall:
    """More of a material is being handed over than the warehouse is known to hold."""

    material_id: UUID
    reference: str
    name: str
    unit: str
    wanted: Decimal
    available: Decimal


def _warehouse(ctx: AccessContext, location_id: UUID) -> Location:
    location = (
        Location.objects.visible_to(ctx, POST)
        .filter(pk=location_id, kind=Location.Kind.WAREHOUSE)
        .first()
    )
    if location is None:
        raise NotInScope(POST)
    return location


def warehouse_stock(
    ctx: AccessContext, location_id: UUID, material_ids: Collection[UUID]
) -> dict[UUID, Decimal]:
    """What this warehouse is known to hold of each material (404 outside its scope)."""
    location = _warehouse(ctx, location_id)
    ids = list(material_ids)
    received = dict(
        GoodsReceiptLine.objects.filter(
            receipt__location=location,
            receipt__status=GoodsReceipt.Status.POSTED,
            po_line__material_id__in=ids,
        )
        .values("po_line__material_id")
        .annotate(total=Sum("qty_received"))
        .values_list("po_line__material_id", "total")
    )
    handed = dict(
        HandoverLine.objects.filter(
            handover__source_location=location,
            handover__kind=Handover.Kind.FROM_WAREHOUSE,
            handover__status=Handover.Status.POSTED,
            request_line__material_id__in=ids,
        )
        .values("request_line__material_id")
        .annotate(total=Sum("qty"))
        .values_list("request_line__material_id", "total")
    )
    return {
        material: max(ZERO, received.get(material, ZERO) - handed.get(material, ZERO))
        for material in ids
    }


def stock_warnings(
    ctx: AccessContext, location_id: UUID, quantities: Mapping[UUID, Decimal]
) -> list[Shortfall]:
    """The materials of a handover (request line → quantity) beyond the warehouse's stock.

    Request lines outside ``handover.post`` are ignored, like lines without a material.
    """
    lines = list(
        MaterialRequestLine.objects.visible_to(ctx, POST)
        .filter(pk__in=list(quantities))
        .select_related("material", "unit")
    )
    wanted: dict[UUID, Decimal] = {}
    materials: dict[UUID, tuple[Material, str]] = {}
    for line in lines:
        if line.material is not None:
            materials[line.material.pk] = (line.material, line.unit.code)
            wanted[line.material.pk] = wanted.get(line.material.pk, ZERO) + quantities[line.pk]
    stock = warehouse_stock(ctx, location_id, wanted)
    shortfalls = [
        Shortfall(
            material_id=material.pk,
            reference=material.reference,
            name=material.name,
            unit=unit,
            wanted=wanted[material.pk],
            available=stock[material.pk],
        )
        for material, unit in materials.values()
        if wanted[material.pk] > stock[material.pk]
    ]
    return sorted(shortfalls, key=lambda item: item.reference)


# --- handovers ----------------------------------


def handovers(
    ctx: AccessContext,
    *,
    status: str = "",
    kind: str = "",
    project_id: UUID | None = None,
    q: str = "",
) -> QuerySet[Handover]:
    """Handovers the user may see, newest first. ``q`` looks at the number, the project's
    code or name and the receiver's typed name."""
    rows = Handover.objects.visible_to(ctx, VIEW).select_related(
        "project", "branch__company", "source_location", "received_by"
    )
    if status:
        rows = rows.filter(status=status)
    if kind:
        rows = rows.filter(kind=kind)
    if project_id is not None:
        rows = rows.filter(project_id=project_id)
    if q:
        rows = rows.filter(
            Q(number__icontains=q)
            | Q(project__code__icontains=q)
            | Q(project__name__icontains=q)
            | Q(received_by_name__icontains=q)
        )
    return rows


def list_projects(ctx: AccessContext) -> QuerySet[Project]:
    """The projects whose handovers the user may filter by."""
    return Project.objects.visible_to(ctx, VIEW).order_by("code")


def handover(ctx: AccessContext, handover_id: UUID) -> Handover:
    found = (
        Handover.objects.visible_to(ctx, VIEW)
        .filter(pk=handover_id)
        .select_related(
            "project",
            "branch__company",
            "source_location",
            "received_by",
            "handed_by",
            "voided_by",
            "goods_receipt",
        )
        .first()
    )
    if found is None:
        raise NotInScope(VIEW)
    return found


def can_post(ctx: AccessContext) -> bool:
    return ctx.has(POST)


def can_void(ctx: AccessContext, of: Handover) -> bool:
    """Only a posted warehouse handover: a direct one goes with its receipt (I8)."""
    return (
        of.status == Handover.Status.POSTED
        and of.kind == Handover.Kind.FROM_WAREHOUSE
        and ctx.can(VOID, of)
    )


def handover_lines(ctx: AccessContext, of: Handover) -> list[HandoverLine]:
    return list(
        HandoverLine.objects.visible_to(ctx, VIEW)
        .filter(handover=of)
        .select_related("request_line__request", "request_line__material", "request_line__unit")
        .order_by("request_line__request__number", "request_line__line_no")
    )


# --- the handover form ----------------------------------


def posting_projects(ctx: AccessContext) -> QuerySet[Project]:
    """Active projects with a live line the user may hand material over for."""
    lines = MaterialRequestLine.objects.visible_to(ctx, POST).filter(request_ledger.open_q())
    return Project.objects.filter(
        pk__in=lines.values("project_id"), status=Project.Status.ACTIVE
    ).order_by("code")


def posting_project(ctx: AccessContext, project_id: UUID) -> Project:
    found = posting_projects(ctx).filter(pk=project_id).select_related("branch__company").first()
    if found is None:
        raise NotInScope(POST)
    return found


def posting_request(ctx: AccessContext, request_id: UUID) -> MaterialRequest:
    found = (
        MaterialRequest.objects.visible_to(ctx, POST)
        .filter(pk=request_id, status=MaterialRequest.Status.APPROVED)
        .select_related("project")
        .first()
    )
    if found is None:
        raise NotInScope(POST)
    return found


@dataclass(frozen=True)
class OpenLine:
    """A live request line with what is still to hand over (I5: A - D)."""

    line: MaterialRequestLine
    delivered: Decimal
    outstanding: Decimal


def open_lines(
    ctx: AccessContext,
    project: Project,
    *,
    request: MaterialRequest | None = None,
) -> list[OpenLine]:
    """The project's live lines with something left to hand over: those of ``request``, or
    of every request handled by the project's procurement branch (C8: one per handover)."""
    lines = (
        MaterialRequestLine.objects.visible_to(ctx, POST)
        .filter(request_ledger.open_q(), project=project)
        .select_related("request", "material", "unit")
        .order_by("request__number", "line_no")
    )
    if request is not None:
        lines = lines.filter(request=request)
    else:
        lines = lines.filter(request__procurement_branch_id=project.procurement_branch_id)
    listed = list(lines)
    flows = request_ledger.flows([line.pk for line in listed])
    rows = [
        OpenLine(
            line,
            flows[line.pk].delivered,
            (line.approved_qty or ZERO) - flows[line.pk].delivered,
        )
        for line in listed
    ]
    return [row for row in rows if row.outstanding > 0]


def awaiting_handover(ctx: AccessContext) -> list[request_ledger.LineLedger]:
    """Open lines with goods received and not yet handed over (the ledger's *awaiting
    handover*), within ``handover.post`` scope, ordered by request (M7.2 dashboard)."""
    lines = (
        MaterialRequestLine.objects.visible_to(ctx, POST)
        .filter(request_ledger.open_q())
        .select_related("request", "project", "material", "unit")
        .order_by("request__number", "line_no")
    )
    return [entry for entry in request_ledger.line_ledgers(lines) if entry.awaiting_handover > 0]


def sources(ctx: AccessContext, branch_id: UUID) -> QuerySet[Location]:
    """The warehouses material for ``branch_id``'s requests may come from (C7): active ones
    of that branch or of a branch it manages, the user's scope permitting."""
    return (
        Location.objects.visible_to(ctx, POST)
        .filter(
            Q(branch_id=branch_id) | Q(branch__managed_by_id=branch_id),
            kind=Location.Kind.WAREHOUSE,
            is_active=True,
            branch__is_active=True,
            branch__company__is_active=True,
        )
        .select_related("branch")
        .order_by("branch__name", "name")
    )


def receivers(project: Project) -> list[User]:
    """The people who may take material for the project: active accounts holding
    ``project.view`` on it, among the project's branch scope and its assignees."""
    branches = [project.branch_id]
    if project.branch.managed_by_id is not None:
        branches.append(project.branch.managed_by_id)
    members = Membership.objects.filter(valid_now(), branch_id__in=branches).values("user_id")
    assigned = ProjectAssignment.objects.filter(valid_now(), project=project).values("user_id")
    candidates = User.objects.filter(
        Q(pk__in=members) | Q(pk__in=assigned), is_active=True, offboarded_at__isnull=True
    ).order_by("full_name", "email")
    return [
        user
        for user in candidates
        if object_in_scope(project, AccessContext.for_user(user).scope_for(Capability.PROJECT_VIEW))
    ]

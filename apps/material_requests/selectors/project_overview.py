"""The project's materials: what is still outstanding for the worksite (M6.5)."""

from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal
from uuid import UUID

from django.db.models import Case, CharField, Min, Q, QuerySet, Value, When
from django.db.models.functions import Cast, Coalesce, Concat, Lower

from apps.access.context import AccessContext
from apps.access.exceptions import NotInScope
from apps.core.listing import seek_after
from apps.material_requests import ledger
from apps.material_requests.models import MaterialRequestLine
from apps.material_requests.selectors.requester import VIEW
from apps.projects.models import Project


@dataclass(frozen=True)
class ProjectMaterial:
    """One material (or free-text description) over the open lines of a project."""

    description: str
    reference: str
    unit_code: str
    requests: int
    approved: Decimal
    received: Decimal
    pending: Decimal
    delivered: Decimal
    awaiting_handover: Decimal
    outstanding: Decimal


def project_for_overview(ctx: AccessContext, project_id: UUID) -> Project:
    found = Project.objects.visible_to(ctx, VIEW).filter(pk=project_id).first()
    if found is None:
        raise NotInScope(VIEW)
    return found


def _project_material(entries: list[ledger.LineLedger]) -> ProjectMaterial:
    first = entries[0].line
    material = first.material
    return ProjectMaterial(
        description=material.name if material else first.description,
        reference=material.reference if material else "",
        unit_code=first.unit.code,
        requests=len({entry.line.request_id for entry in entries}),
        approved=sum((entry.approved for entry in entries), ledger.ZERO),
        received=sum((entry.flows.received for entry in entries), ledger.ZERO),
        pending=sum((entry.flows.pending for entry in entries), ledger.ZERO),
        delivered=sum((entry.flows.delivered for entry in entries), ledger.ZERO),
        awaiting_handover=sum((entry.awaiting_handover for entry in entries), ledger.ZERO),
        outstanding=sum((entry.outstanding for entry in entries), ledger.ZERO),
    )


MATERIAL_ORDER = ("seek_label", "seek_group", "unit_id")
"""A material of the project: its name (or a free text), the group, the unit."""


def _by_material(rows: QuerySet[MaterialRequestLine]) -> QuerySet[MaterialRequestLine]:
    """``rows`` with the fields of ``MATERIAL_ORDER``: a catalog material groups by its id, a
    free text by its words whatever their case; both with the unit."""
    return rows.annotate(
        seek_label=Lower(Coalesce("material__name", "free_text")),
        seek_group=Case(
            When(
                material__isnull=False, then=Concat(Value("m:"), Cast("material_id", CharField()))
            ),
            default=Concat(Value("t:"), Lower("free_text")),
            output_field=CharField(),
        ),
    )


class ProjectMaterials:
    """Per material, the open lines of the requests the user may see for this project, a
    page at a time (M6.5, M7d.4).

    Alphabetical by material. Only open lines count (approved requests, live lines), so a
    closed or cancelled request and a line closed short no longer weigh on the worksite.
    ``q`` looks at the description and the reference (in SQL); ``outstanding_only`` drops the
    materials with nothing left to arrive or hand over, as their ledgers are worked out.
    ``seek`` reads the next materials from the database, ``window`` works out the ledgers of
    a stretch of them only.
    """

    def __init__(
        self, ctx: AccessContext, of: Project, *, q: str = "", outstanding_only: bool = False
    ) -> None:
        lines = MaterialRequestLine.objects.visible_to(ctx, VIEW).filter(
            ledger.open_q(), request__project=of
        )
        needle = q.strip()
        if needle:
            lines = lines.filter(
                Q(material__name__icontains=needle)
                | Q(material__reference__icontains=needle)
                | Q(material__isnull=True, free_text__icontains=needle)
            )
        self._lines = _by_material(lines)
        self._outstanding_only = outstanding_only
        self._group: dict[str, tuple[str, UUID]] = {}

    def seek(self, cursor: str, limit: int) -> list[str]:
        """The keys of the next materials: the first line id of each (a cursor finds the
        material again from any of its lines)."""
        rows = seek_after(
            self._lines, _by_material(MaterialRequestLine.objects.all()), MATERIAL_ORDER, cursor
        )
        found = rows.values(*MATERIAL_ORDER).annotate(seek_key=Min(Cast("pk", CharField())))
        keys = []
        for row in found[:limit]:
            self._group[row["seek_key"]] = (row["seek_group"], row["unit_id"])
            keys.append(row["seek_key"])
        return keys

    def window(self, chunk: Sequence[str]) -> list[tuple[int, ProjectMaterial]]:
        wanted = {self._group[key] for key in chunk}
        lines = (
            self._lines.filter(
                Q(seek_group__in={group for group, _ in wanted}),
                unit_id__in={unit for _, unit in wanted},
            )
            .select_related("request", "material", "unit")
            .order_by("request__number", "line_no")
        )
        grouped: dict[tuple[str, UUID], list[ledger.LineLedger]] = {}
        for entry in ledger.line_ledgers(lines):
            pair = (getattr(entry.line, "seek_group"), entry.line.unit_id)  # noqa: B009
            if pair in wanted:
                grouped.setdefault(pair, []).append(entry)
        items = {pair: _project_material(entries) for pair, entries in grouped.items()}
        return [
            (index, items[self._group[key]])
            for index, key in enumerate(chunk)
            if self._group[key] in items and self._keeps(items[self._group[key]])
        ]

    def _keeps(self, item: ProjectMaterial) -> bool:
        return not self._outstanding_only or item.outstanding > 0

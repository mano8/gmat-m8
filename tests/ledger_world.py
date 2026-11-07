"""The world of the ledger checks (M8.1): A-MAD buys cable for approved requests of three
projects, built on the shared A/B/C fixture (``tests/org.py``).

    SOL-L-00001  hospital  NORMAL  X: 10 m cable (purchase)  S: 4 m cable (stock)
    SOL-L-00002  hospital  HIGH    Y: 8 m cable
    SOL-L-00003  MAD-2     NORMAL  Z: 6 m cable              V: 5 m cable 4 mm²
    SOL-L-00004  SEV-2     LOW     W: 7 m cable

Every request is procured by A-MAD, so every line can share an order and the four
projects' lines form one component. The requests are written with the ORM, as approved
(the review is not what these checks are about); everything after that goes through the
services, as A-MAD's material manager.
"""

from dataclasses import dataclass
from datetime import timedelta
from decimal import Decimal
from uuid import UUID

from django.utils import timezone

from apps.access.context import AccessContext
from apps.access.principals import SessionPrincipal
from apps.catalog.models import Material, UnitOfMeasure
from apps.locations.models import Location
from apps.material_requests.models import MaterialRequest, MaterialRequestLine
from apps.projects.models import Project
from tests.org import Org, People
from tests.support import principal_for

Sourcing = MaterialRequestLine.Sourcing


@dataclass(frozen=True)
class LedgerWorld:
    org: Org
    people: People
    buyer: SessionPrincipal  # A-MAD's material manager: buys, receives, hands over, edits
    ctx: AccessContext  # the group administrator's: reads every warehouse (I9)
    materials: tuple[Material, Material]  # cable, cable 4 mm² (same unit)
    projects: tuple[Project, Project, Project]  # hospital, MAD-2, SEV-2
    lines: dict[str, MaterialRequestLine]

    @property
    def warehouses(self) -> dict[str, Location]:
        """A-MAD's own, B-MAD's (managed by A-MAD) and A-SEV's (not in A-MAD's scope)."""
        return self.org.warehouses

    def worksite(self, project_id: UUID) -> Location:
        return Location.objects.get(project_id=project_id, kind=Location.Kind.WORKSITE)


def _third(org: Org) -> Project:
    """MAD-2: a project of A-MAD, bought by A-MAD, with its worksite."""
    project = Project.objects.create(
        code="MAD-2",
        name="MAD-2",
        branch=org.a_mad,
        procurement_branch=org.a_mad,
        status=Project.Status.ACTIVE,
    )
    Location.objects.create(
        kind=Location.Kind.WORKSITE, branch=org.a_mad, project=project, name="site MAD-2"
    )
    return project


@dataclass(frozen=True)
class _Line:
    name: str
    material: Material
    quantity: str
    sourcing: str = Sourcing.PURCHASE


def _request(
    people: People, project: Project, number: int, priority: str, lines: list[_Line]
) -> dict[str, MaterialRequestLine]:
    now = timezone.now()
    request = MaterialRequest.objects.create(
        project=project,
        procurement_branch_id=project.procurement_branch_id,
        requested_by=people.tech_mad,
        status=MaterialRequest.Status.APPROVED,
        number=f"SOL-L-{number:05d}",
        submitted_at=now - timedelta(days=1) + timedelta(minutes=number),
        decided_at=now,
        priority=priority,
    )
    return {
        line.name: MaterialRequestLine.objects.create(
            request=request,
            project=project,
            line_no=line_no,
            material=line.material,
            unit_id=line.material.unit_id,
            requested_qty=Decimal(line.quantity),
            approved_qty=Decimal(line.quantity),
            decision=MaterialRequestLine.Decision.APPROVED,
            decided_by=people.mm_mad,
            decided_at=now,
            sourcing=line.sourcing,
        )
        for line_no, line in enumerate(lines, start=1)
    }


def build_ledger_world(org: Org, people: People) -> LedgerWorld:
    # ``get_or_create``: a transactional test flushes the seeded units (M8.1 races).
    metres, _ = UnitOfMeasure.objects.get_or_create(code="M", defaults={"name": "m", "decimals": 2})
    cable = Material.objects.create(reference="CAB-25", name="Cable 2,5", unit=metres)
    cable4 = Material.objects.create(reference="CAB-40", name="Cable 4", unit=metres)
    third = _third(org)
    priority = MaterialRequest.Priority
    lines = {
        **_request(
            people,
            org.hospital,
            1,
            priority.NORMAL,
            [_Line("X", cable, "10"), _Line("S", cable, "4", Sourcing.STOCK)],
        ),
        **_request(people, org.hospital, 2, priority.HIGH, [_Line("Y", cable, "8")]),
        **_request(
            people, third, 3, priority.NORMAL, [_Line("Z", cable, "6"), _Line("V", cable4, "5")]
        ),
        **_request(people, org.sev_for_mad, 4, priority.LOW, [_Line("W", cable, "7")]),
    }
    return LedgerWorld(
        org=org,
        people=people,
        buyer=principal_for(people.mm_mad),
        ctx=AccessContext.for_user(people.group_admin),
        materials=(cable, cable4),
        projects=(org.hospital, third, org.sev_for_mad),
        lines=lines,
    )

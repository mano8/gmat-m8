"""Fixtures of the dashboard tests: the purchasing and receiving fixtures (approved request
lines, orders and receipts through the services) and a context per person."""

from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest
from django.utils import timezone

from apps.access.context import AccessContext
from apps.material_requests.models import MaterialRequest, MaterialRequestLine
from apps.projects.models import Project
from tests.org import Org, People
from tests.unit.handovers.conftest import hand_over  # noqa: F401 - fixture
from tests.unit.purchasing.conftest import (  # noqa: F401 - fixtures used by these tests
    breaker,
    buy,
    cable,
    make_line,
    mm,
)
from tests.unit.receiving.conftest import receive, third  # noqa: F401 - fixtures


@pytest.fixture
def ctx(people: People) -> AccessContext:
    """A-MAD's material manager: buys for A-MAD and B-MAD."""
    return AccessContext.for_user(people.mm_mad)


@pytest.fixture
def other(people: People) -> AccessContext:
    """A-SEV's material manager: sees nothing of A-MAD's documents."""
    return AccessContext.for_user(people.mm_sev)


Status = MaterialRequest.Status
Decision = MaterialRequestLine.Decision
HOUR = timedelta(hours=1)


@dataclass(frozen=True)
class Spec:
    """What ``Requests.make`` writes: the request's state and times, its lines' decisions."""

    project: Project | None = None
    by: Any = None
    status: str = Status.SUBMITTED
    submitted: datetime | None = None
    decided: datetime | None = None
    closed: datetime | None = None
    lines: tuple[str, ...] = ("PENDING",)

    @property
    def drafted(self) -> bool:
        return self.status in (Status.DRAFT, Status.CANCELLED)


class Requests:
    """Requests written directly, with the times a chart reads."""

    def __init__(self, org: Org, people: People, material: Any) -> None:
        self.org, self.people, self.cable = org, people, material
        self.count = 0

    def make(self, **options: Any) -> MaterialRequest:
        spec = Spec(**options)
        self.count += 1
        project = spec.project or self.org.hospital
        submitted = spec.submitted or timezone.now() - HOUR
        request = MaterialRequest.objects.create(
            project=project,
            procurement_branch_id=project.procurement_branch_id,
            requested_by=spec.by or self.people.tech_mad,
            status=spec.status,
            number=None if spec.drafted else f"SOL-D-{self.count:05d}",
            submitted_at=None if spec.drafted else submitted,
            decided_at=spec.decided,
            closed_at=spec.closed,
            close_mode="AUTO" if spec.closed else "",
        )
        for index, decision in enumerate(spec.lines, start=1):
            self._line(request, index, decision, spec.decided or timezone.now())
        return request

    def _line(self, request: MaterialRequest, index: int, decision: str, at: datetime) -> None:
        decided = decision != "PENDING"
        MaterialRequestLine.objects.create(
            request=request,
            project_id=request.project_id,
            line_no=index,
            material=self.cable,
            unit_id=self.cable.unit_id,
            requested_qty=Decimal("5"),
            approved_qty=Decimal("5") if decision == Decision.APPROVED else None,
            decision=decision,
            decided_by=self.people.mm_mad if decided else None,
            decided_at=at if decided else None,
        )


@pytest.fixture
def requests(org: Org, people: People, cable: Any) -> Requests:  # noqa: F811 - the fixture
    return Requests(org, people, cable)

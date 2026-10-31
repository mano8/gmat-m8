"""Fixtures for the material request tests: catalog rows and requests in any state."""

from collections.abc import Callable
from decimal import Decimal
from typing import Any

import pytest
from django.utils import timezone

from apps.accounts.models import User
from apps.catalog.models import Material, UnitOfMeasure
from apps.material_requests.models import MaterialRequest, MaterialRequestLine
from apps.projects.models import Project
from tests.org import Org, People

Status = MaterialRequest.Status


@pytest.fixture
def cable(db: None) -> Material:
    """A material measured in metres (two decimals)."""
    return Material.objects.create(
        reference="CAB-25", name="Cable 2,5", unit=UnitOfMeasure.objects.get(code="M")
    )


@pytest.fixture
def breaker(db: None) -> Material:
    """A material counted in units (no decimals)."""
    return Material.objects.create(
        reference="DIF-40", name="Diferencial 40A", unit=UnitOfMeasure.objects.get(code="UD")
    )


type MakeRequest = Callable[..., MaterialRequest]


@pytest.fixture
def make_request(org: Org, people: People, cable: Material) -> MakeRequest:
    """A request written directly in any state (fixtures only), with one cable line.

    Submitted states get a number and a submit time; ``decided`` decides the line.
    """
    count = iter(range(1, 1000))

    def make(
        status: str = Status.DRAFT,
        *,
        user: User | None = None,
        project: Project | None = None,
        lines: int = 1,
        decided: bool = False,
        **fields: Any,
    ) -> MaterialRequest:
        project = project or org.hospital
        submitted = status not in {Status.DRAFT, Status.CANCELLED}
        now = timezone.now()
        closed = {"closed_at": now, "close_mode": "AUTO"} if status == Status.CLOSED else {}
        request = MaterialRequest.objects.create(
            project=project,
            procurement_branch_id=project.procurement_branch_id,
            requested_by=user or people.tech_mad,
            status=status,
            number=f"SOL-T-{next(count):05d}" if submitted else None,
            submitted_at=now if submitted else None,
            **closed,
            **fields,
        )
        for line_no in range(1, lines + 1):
            decision = (
                {
                    "decision": MaterialRequestLine.Decision.APPROVED,
                    "approved_qty": Decimal("5.00"),
                    "decided_by": people.mm_mad,
                    "decided_at": now,
                }
                if decided
                else {}
            )
            MaterialRequestLine.objects.create(
                request=request,
                project=project,
                line_no=line_no,
                material=cable,
                unit=cable.unit,
                requested_qty=Decimal("10.00"),
                **decision,
            )
        return request

    return make

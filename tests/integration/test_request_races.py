"""Request lifecycle races with real connections (plan §4.7, §4.6 C9, §4.8).

- An ``add_line`` racing a ``submit`` is serialized by the header lock (L2): either the line
  is in the submitted request, or it is refused because the request is no longer a draft.
- A material's unit change racing the first line that references it ends with exactly one
  of the two committed and every line in the material's unit (C9, held by the composite
  foreign key and its ``FOR KEY SHARE`` lock; no material lock).
- A double submit with one idempotency key yields one request number.

The first operation is slowed inside its transaction (a subscriber of its own event sleeps
while its locks are held); the second starts while it waits.
"""

import time
from collections.abc import Callable, Iterator
from decimal import Decimal
from typing import Any

import pytest
from django.core.exceptions import ValidationError

from apps.catalog.models import Material, UnitOfMeasure
from apps.catalog.services import update_material
from apps.core import events
from apps.core.idempotency import issue_key
from apps.core.models import DocumentSequence
from apps.material_requests import services
from apps.material_requests.models import MaterialRequest, MaterialRequestLine
from tests.integration.conftest import run_concurrently
from tests.org import Org, People
from tests.support import principal_for

pytestmark = [pytest.mark.integration, pytest.mark.django_db(transaction=True)]

HOLD = 0.4  # seconds the first operation keeps its locks
HEAD_START = 0.15  # seconds before the second operation starts


def outcome(results: list[Any]) -> list[str]:
    return [type(result).__name__ for result in results]


def slowly(event_name: str) -> Callable[[], None]:
    """Make the publisher of ``event_name`` hold its transaction's locks for ``HOLD``."""

    def hold(event: events.Event) -> None:
        time.sleep(HOLD)

    return events.subscribe(event_name, hold)


def second_starts_later(*operations: Callable[[], Any]) -> list[Any]:
    def run(index: int) -> Any:
        if index:
            time.sleep(HEAD_START)
        return operations[index]()

    return run_concurrently(len(operations), run)


def unit(code: str, decimals: int) -> UnitOfMeasure:
    """A seeded unit; transactional tests flush the seed, so it is created again if needed."""
    found, _ = UnitOfMeasure.objects.get_or_create(
        code=code, defaults={"name": code, "decimals": decimals}
    )
    return found


@pytest.fixture
def cable(db: None) -> Material:
    return Material.objects.create(reference="CAB-25", name="Cable 2,5", unit=unit("M", 2))


@pytest.fixture
def draft(org: Org, people: People, cable: Material) -> MaterialRequest:
    request = MaterialRequest.objects.create(
        project=org.hospital, procurement_branch=org.a_mad, requested_by=people.tech_mad
    )
    MaterialRequestLine.objects.create(
        request=request,
        project=org.hospital,
        line_no=1,
        material=cable,
        unit=cable.unit,
        requested_qty=Decimal("10"),
    )
    return request


@pytest.fixture
def undo() -> Iterator[list[Callable[[], None]]]:
    registered: list[Callable[[], None]] = []
    yield registered
    for unsubscribe in registered:
        unsubscribe()


# --- add_line vs submit ---------------------------------------------------------------


def test_add_line_waits_for_a_submit_and_is_refused(
    people: People, draft: MaterialRequest, cable: Material, undo: list[Callable[[], None]]
) -> None:
    principal = principal_for(people.tech_mad)
    undo.append(slowly("request.submitted"))
    results = second_starts_later(
        lambda: services.submit(principal, draft.pk),
        lambda: services.add_line(
            principal, draft.pk, material_id=cable.pk, requested_qty=Decimal("1")
        ),
    )
    assert outcome(results) == ["MaterialRequest", "ValidationError"]
    assert results[1].code == "not_draft"
    assert draft.lines.count() == 1
    assert MaterialRequest.objects.get(pk=draft.pk).status == MaterialRequest.Status.SUBMITTED


def test_submit_waits_for_an_add_line_and_includes_it(
    people: People, draft: MaterialRequest, cable: Material, undo: list[Callable[[], None]]
) -> None:
    principal = principal_for(people.tech_mad)
    undo.append(slowly("request_line.added"))
    results = second_starts_later(
        lambda: services.add_line(
            principal, draft.pk, material_id=cable.pk, requested_qty=Decimal("1")
        ),
        lambda: services.submit(principal, draft.pk),
    )
    assert outcome(results) == ["MaterialRequestLine", "MaterialRequest"]
    submitted = MaterialRequest.objects.get(pk=draft.pk)
    assert submitted.status == MaterialRequest.Status.SUBMITTED
    assert submitted.lines.count() == 2


# --- a unit change vs the first reference (C9) ----------------------------------------


def units_consistent(material: Material) -> bool:
    unit_id = Material.objects.values_list("unit_id", flat=True).get(pk=material.pk)
    lines = MaterialRequestLine.objects.filter(material=material)
    return not lines.exclude(unit_id=unit_id).exists()


@pytest.fixture
def unreferenced(org: Org, people: People, cable: Material) -> MaterialRequest:
    """A draft with no line yet: the material is not referenced by any document."""
    return MaterialRequest.objects.create(
        project=org.hospital, procurement_branch=org.a_mad, requested_by=people.tech_mad
    )


def test_a_unit_change_first_refuses_the_racing_line(
    people: People,
    unreferenced: MaterialRequest,
    cable: Material,
    undo: list[Callable[[], None]],
) -> None:
    kg = unit("KG", 3)
    undo.append(slowly("material.updated"))
    results = second_starts_later(
        lambda: update_material(principal_for(people.mm_mad), cable.pk, unit_id=kg.pk),
        lambda: services.add_line(
            principal_for(people.tech_mad),
            unreferenced.pk,
            material_id=cable.pk,
            requested_qty=Decimal("5"),
        ),
    )
    assert outcome(results) == ["Material", "ValidationError"]
    assert "material" in results[1].error_dict
    assert Material.objects.get(pk=cable.pk).unit_id == kg.pk
    assert not unreferenced.lines.exists()
    assert units_consistent(cable)


def test_a_first_reference_first_refuses_the_unit_change(
    people: People,
    unreferenced: MaterialRequest,
    cable: Material,
    undo: list[Callable[[], None]],
) -> None:
    kg = unit("KG", 3)
    undo.append(slowly("request_line.added"))
    results = second_starts_later(
        lambda: services.add_line(
            principal_for(people.tech_mad),
            unreferenced.pk,
            material_id=cable.pk,
            requested_qty=Decimal("5"),
        ),
        lambda: update_material(principal_for(people.mm_mad), cable.pk, unit_id=kg.pk),
    )
    assert outcome(results) == ["MaterialRequestLine", "ValidationError"]
    assert "unit" in results[1].error_dict
    assert Material.objects.get(pk=cable.pk).unit_id == cable.unit_id
    assert unreferenced.lines.count() == 1
    assert units_consistent(cable)


# --- submit twice ---------------------------------------------------------------------


def test_a_double_submit_with_one_key_gets_one_number(
    people: People, draft: MaterialRequest
) -> None:
    principal = principal_for(people.tech_mad)
    key = issue_key(people.tech_mad.pk, "request.submit")
    results = run_concurrently(
        2, lambda index: services.submit(principal, draft.pk, idempotency_key=key)
    )
    assert outcome(results) == ["MaterialRequest", "MaterialRequest"]
    assert results[0].number == results[1].number
    assert DocumentSequence.objects.get().next_value == 2


def test_two_submits_without_a_key_submit_once(people: People, draft: MaterialRequest) -> None:
    principal = principal_for(people.tech_mad)
    results = run_concurrently(2, lambda index: services.submit(principal, draft.pk))
    assert sorted(outcome(results)) == ["MaterialRequest", "ValidationError"]
    refused = next(result for result in results if isinstance(result, ValidationError))
    assert refused.code == "transition"
    assert DocumentSequence.objects.get().next_value == 2


def test_a_cancel_racing_a_decision_takes_the_stricter_path(
    people: People, draft: MaterialRequest, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A line decided between choosing the path and locking: the service runs again.

    The retry wrapper rolls back and re-runs; now ``request.cancel_any`` is needed, which a
    technician lacks.
    """
    MaterialRequest.objects.filter(pk=draft.pk).update(
        status=MaterialRequest.Status.SUBMITTED, number="SOL-T-1", submitted_at=draft.created_at
    )
    calls = iter([False])
    monkeypatch.setattr(services, "_decided", lambda request: next(calls, True))
    results = run_concurrently(
        1, lambda index: services.cancel(principal_for(people.tech_mad), draft.pk)
    )
    assert outcome(results) == ["AccessDenied"]

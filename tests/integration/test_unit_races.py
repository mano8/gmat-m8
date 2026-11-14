"""Two changes of units cannot combine into two units with one code (Q72, ADR-0030).

The code is unique ignoring case. Run concurrently, exactly one of two creations of the same
code, or of two renames to the same code, commits, and the other is a refusal on the field (a
``ValidationError`` the dialog shows), never an ``IntegrityError`` that reaches the person.
"""

import time
from collections.abc import Callable
from typing import Any

import pytest
from django.core.exceptions import ValidationError

from apps.access.principals import SessionPrincipal
from apps.access.roles import Role
from apps.accounts.models import User
from apps.catalog import services
from apps.catalog.models import UnitOfMeasure
from apps.core import events
from tests.integration.conftest import run_concurrently
from tests.support import grant, principal_for

pytestmark = [pytest.mark.integration, pytest.mark.serial, pytest.mark.django_db(transaction=True)]


@pytest.fixture
def principal(make_user: Callable[..., User]) -> SessionPrincipal:
    admin = make_user("admin@example.org")
    grant(admin, Role.GROUP_ADMIN)
    return principal_for(admin)


def outcome(results: list[Any]) -> list[str]:
    return sorted(type(result).__name__ for result in results)


def test_two_creations_of_one_code(principal: SessionPrincipal) -> None:
    def slow(event: events.Event) -> None:
        time.sleep(0.3)  # the other creation arrives while this one is open

    undo = events.subscribe("unit.created", slow)
    try:
        results = run_concurrently(
            2,
            lambda index: services.create_unit(
                principal, code=("zz", "ZZ")[index], name=f"Unit {index}"
            ),
        )
    finally:
        undo()
    assert outcome(results) == ["UnitOfMeasure", "ValidationError"]
    assert "code" in next(r for r in results if isinstance(r, ValidationError)).error_dict
    assert UnitOfMeasure.objects.filter(code__iexact="zz").count() == 1


def test_two_renames_to_one_code(principal: SessionPrincipal) -> None:
    first = UnitOfMeasure.objects.create(code="AAA", name="First")
    second = UnitOfMeasure.objects.create(code="BBB", name="Second")
    units = [first, second]

    def slow(event: events.Event) -> None:
        time.sleep(0.3)

    undo = events.subscribe("unit.updated", slow)
    try:
        results = run_concurrently(
            2, lambda index: services.update_unit(principal, units[index].pk, code="NEW")
        )
    finally:
        undo()
    assert outcome(results) == ["UnitOfMeasure", "ValidationError"]
    assert UnitOfMeasure.objects.filter(code="NEW").count() == 1

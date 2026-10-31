"""Gapless document numbers (apps.core.sequences); the race is in tests/integration."""

import pytest
from django.db import IntegrityError, transaction

from apps.core.models import DocumentSequence
from apps.core.sequences import SequenceOutsideTransaction, format_number, next_number

pytestmark = pytest.mark.django_db


def test_numbers_start_at_one_and_increase() -> None:
    with transaction.atomic():
        assert [next_number("PO:A:2026") for _ in range(3)] == [1, 2, 3]
    assert DocumentSequence.objects.get(scope="PO:A:2026").next_value == 4


def test_scopes_are_independent() -> None:
    with transaction.atomic():
        assert next_number("SOL:B1:2026") == 1
        assert next_number("SOL:B2:2026") == 1
        assert next_number("SOL:B1:2026") == 2


def test_a_rollback_returns_the_number() -> None:
    with transaction.atomic():
        assert next_number("REC:B1:2026") == 1
    with pytest.raises(RuntimeError), transaction.atomic():
        assert next_number("REC:B1:2026") == 2
        raise RuntimeError("the document failed")
    with transaction.atomic():
        assert next_number("REC:B1:2026") == 2


@pytest.mark.django_db(transaction=True)
def test_autocommit_is_refused() -> None:
    with pytest.raises(SequenceOutsideTransaction):
        next_number("ENT:B1:2026")
    assert not DocumentSequence.objects.exists()


@pytest.mark.parametrize("scope", ["", "x" * 101])
def test_scope_length_is_checked(scope: str) -> None:
    with pytest.raises(ValueError, match="scope"), transaction.atomic():
        next_number(scope)


def test_next_value_is_at_least_one() -> None:
    with pytest.raises(IntegrityError), transaction.atomic():
        DocumentSequence.objects.create(scope="BAD", next_value=0)


def test_format() -> None:
    assert format_number("SOL", "B1", 2026, 7) == "SOL-B1-2026-00007"
    assert format_number("PED", "A", 2026, 123456) == "PED-A-2026-123456"
    assert str(DocumentSequence(scope="PO:A:2026", next_value=3)) == "PO:A:2026 → 3"

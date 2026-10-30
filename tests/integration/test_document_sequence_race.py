"""Concurrent ``next_number()`` calls never collide or leave gaps (plan §4.7 L9)."""

import random
import time

import pytest
from django.db import transaction

from apps.core.models import DocumentSequence
from apps.core.sequences import next_number
from tests.integration.conftest import run_concurrently

pytestmark = [pytest.mark.integration, pytest.mark.django_db(transaction=True)]

WORKERS = 8
PER_WORKER = 5


def test_concurrent_numbers_are_unique_and_gapless() -> None:
    """The scope's row does not exist yet: the first-use insert races too."""

    def take(index: int) -> list[int]:
        taken = []
        for _ in range(PER_WORKER):
            with transaction.atomic():
                taken.append(next_number("PO:A:2026"))
                time.sleep(random.uniform(0, 0.005))  # noqa: S311 - hold the lock a little
        return taken

    results = run_concurrently(WORKERS, take)
    numbers = [number for result in results for number in result]
    assert sorted(numbers) == list(range(1, WORKERS * PER_WORKER + 1))
    assert DocumentSequence.objects.get(scope="PO:A:2026").next_value == WORKERS * PER_WORKER + 1


def test_rolled_back_numbers_are_reused_not_skipped() -> None:
    """Half the workers fail after taking a number; the committed numbers stay gapless."""

    class Failed(Exception):
        pass

    def take(index: int) -> int | None:
        try:
            with transaction.atomic():
                number = next_number("SOL:B1:2026")
                time.sleep(0.01)
                if index % 2:
                    raise Failed
                return number
        except Failed:
            return None

    results = run_concurrently(WORKERS, take)
    committed = sorted(number for number in results if number is not None)
    assert committed == list(range(1, WORKERS // 2 + 1))
    assert DocumentSequence.objects.get(scope="SOL:B1:2026").next_value == WORKERS // 2 + 1

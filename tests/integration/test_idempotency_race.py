"""A simultaneous double submit runs the command once (plan §4.8).

The second insert of the same (actor, key) waits on the unique index until the first
transaction commits, then replays its result.
"""

import time
import uuid
from collections.abc import Callable
from decimal import Decimal
from uuid import UUID

import pytest
from django.db import transaction

from apps.accounts.models import User
from apps.core.idempotency import IdempotentResult, idempotent, issue_key
from apps.core.models import DocumentSequence, IdempotencyRecord
from tests.integration.conftest import run_concurrently

pytestmark = [pytest.mark.integration, pytest.mark.django_db(transaction=True)]

OPERATION = "test.create"


def submit(
    actor: User, key: str, *, fail: bool = False
) -> Callable[[int], IdempotentResult[DocumentSequence]]:
    def execute() -> DocumentSequence:
        created = DocumentSequence.objects.create(scope=f"RACE:{uuid.uuid4()}")
        time.sleep(0.3)  # the duplicate arrives while this transaction is still open
        if fail:
            raise RuntimeError("the first submit failed")
        return created

    def replay(result_type: str, result_id: UUID) -> DocumentSequence:
        return DocumentSequence.objects.get(pk=result_id)

    def run(index: int) -> IdempotentResult[DocumentSequence]:
        with transaction.atomic():
            return idempotent(
                actor_id=actor.pk,
                key=key,
                operation=OPERATION,
                payload={"qty": Decimal("2.000")},
                execute=execute,
                replay=replay,
            )

    return run


def test_double_submit_executes_once(make_user: Callable[..., User]) -> None:
    actor = make_user()
    results = run_concurrently(2, submit(actor, issue_key(actor.pk, OPERATION)))
    assert all(isinstance(result, IdempotentResult) for result in results), results
    assert sorted(result.replayed for result in results) == [False, True]
    assert results[0].value.pk == results[1].value.pk
    assert DocumentSequence.objects.count() == 1
    assert IdempotencyRecord.objects.count() == 1


def test_if_the_first_rolls_back_the_second_runs_as_new(make_user: Callable[..., User]) -> None:
    actor = make_user()
    key = issue_key(actor.pk, OPERATION)
    failing = submit(actor, key, fail=True)
    succeeding = submit(actor, key)

    def run(index: int) -> IdempotentResult[DocumentSequence]:
        if index == 0:
            return failing(index)
        time.sleep(0.1)  # start second, while the first holds the key
        return succeeding(index)

    first, second = run_concurrently(2, run)
    assert isinstance(first, RuntimeError)
    assert isinstance(second, IdempotentResult) and second.replayed is False
    assert DocumentSequence.objects.count() == 1
    assert IdempotencyRecord.objects.get().result_id == second.value.pk

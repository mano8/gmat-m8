"""Idempotent create commands (apps.core.idempotency, plan §4.8).

The simultaneous double submit is in tests/integration/test_idempotency_race.py.
"""

import time
import uuid
from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from enum import Enum, StrEnum
from typing import Any
from uuid import UUID

import pytest
from django.core import signing
from django.db import IntegrityError, transaction
from django.db.models import QuerySet

from apps.accounts.models import User
from apps.core import idempotency
from apps.core.idempotency import (
    IdempotencyConflict,
    IdempotencyKeyExpired,
    IdempotencyKeyInvalid,
    IdempotencyOutsideTransaction,
    IdempotentResult,
    execute_once,
    idempotent,
    issue_key,
    payload_hash,
    purge_expired_records,
    verify_key,
)
from apps.core.models import DocumentSequence, IdempotencyRecord

pytestmark = pytest.mark.django_db

OPERATION = "test.create"
DAY = 24 * 3600


@pytest.fixture
def actor(make_user: Callable[..., User]) -> User:
    return make_user("actor@example.org")


@pytest.fixture
def other(make_user: Callable[..., User]) -> User:
    return make_user("other@example.org")


def issued_ago(monkeypatch: pytest.MonkeyPatch, seconds: int) -> None:
    """Make the signer stamp new keys ``seconds`` in the past."""

    def old(self: signing.TimestampSigner) -> str:
        return signing.b62_encode(int(time.time()) - seconds)

    monkeypatch.setattr(signing.TimestampSigner, "timestamp", old)


class Mode(Enum):
    FAST = 2


class Command:
    """A fake service: creates a ``DocumentSequence`` row per execution."""

    def __init__(self) -> None:
        self.executions = 0
        self.replays: list[tuple[str, UUID]] = []

    def execute(self) -> DocumentSequence:
        self.executions += 1
        return DocumentSequence.objects.create(scope=f"IDEM:{uuid.uuid4()}")

    def replay(self, result_type: str, result_id: UUID) -> DocumentSequence:
        self.replays.append((result_type, result_id))
        return DocumentSequence.objects.get(pk=result_id)

    def run(
        self, actor: User, key: str, payload: dict[str, Any] | None = None
    ) -> IdempotentResult[DocumentSequence]:
        with transaction.atomic():
            return idempotent(
                actor_id=actor.pk,
                key=key,
                operation=OPERATION,
                payload=payload if payload is not None else {"qty": Decimal("1.500")},
                execute=self.execute,
                replay=self.replay,
            )


class TestKeys:
    def test_round_trip(self, actor: User) -> None:
        key = issue_key(actor.pk, OPERATION)
        assert isinstance(verify_key(key, actor_id=actor.pk, operation=OPERATION), UUID)

    def test_each_form_gets_its_own_key(self, actor: User) -> None:
        first = verify_key(issue_key(actor.pk, OPERATION), actor_id=actor.pk, operation=OPERATION)
        second = verify_key(issue_key(actor.pk, OPERATION), actor_id=actor.pk, operation=OPERATION)
        assert first != second

    def test_a_tampered_key_is_refused(self, actor: User) -> None:
        key = issue_key(actor.pk, OPERATION)
        tampered = key[:-1] + ("A" if key[-1] != "A" else "B")
        with pytest.raises(IdempotencyKeyInvalid) as exc:
            verify_key(tampered, actor_id=actor.pk, operation=OPERATION)
        assert exc.value.status_code == 400

    def test_another_actors_key_is_refused(self, actor: User, other: User) -> None:
        with pytest.raises(IdempotencyKeyInvalid):
            verify_key(issue_key(actor.pk, OPERATION), actor_id=other.pk, operation=OPERATION)

    def test_another_operations_key_is_refused(self, actor: User) -> None:
        with pytest.raises(IdempotencyKeyInvalid):
            verify_key(issue_key(actor.pk, OPERATION), actor_id=actor.pk, operation="test.other")

    def test_a_key_signed_without_a_key_id_is_refused(self, actor: User) -> None:
        key = signing.TimestampSigner(salt=idempotency.SALT).sign_object(
            {"k": "not-a-uuid", "a": str(actor.pk), "o": OPERATION}
        )
        with pytest.raises(IdempotencyKeyInvalid):
            verify_key(key, actor_id=actor.pk, operation=OPERATION)

    def test_a_key_older_than_24_hours_has_expired(
        self, actor: User, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        issued_ago(monkeypatch, DAY + 1)
        key = issue_key(actor.pk, OPERATION)
        monkeypatch.undo()
        with pytest.raises(IdempotencyKeyExpired) as exc:
            verify_key(key, actor_id=actor.pk, operation=OPERATION)
        assert exc.value.status_code == 409

    def test_a_key_just_under_24_hours_is_valid(
        self, actor: User, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        issued_ago(monkeypatch, DAY - 60)
        key = issue_key(actor.pk, OPERATION)
        monkeypatch.undo()
        verify_key(key, actor_id=actor.pk, operation=OPERATION)


class TestPayloadHash:
    def test_key_order_does_not_matter(self) -> None:
        assert payload_hash({"a": 1, "b": "x"}) == payload_hash({"b": "x", "a": 1})

    def test_values_matter(self) -> None:
        assert payload_hash({"qty": Decimal("1.500")}) != payload_hash({"qty": Decimal("1.501")})

    def test_supported_types(self) -> None:
        class Priority(StrEnum):
            HIGH = "HIGH"

        payload = {
            "none": None,
            "flag": True,
            "count": 3,
            "qty": Decimal("2.000"),
            "id": uuid.UUID(int=1),
            "day": date(2026, 10, 30),
            "at": datetime(2026, 10, 30, 12, tzinfo=UTC),
            "priority": Priority.HIGH,
            "lines": [{"qty": Decimal("1")}, ("a", "b")],
        }
        assert len(payload_hash(payload)) == 64
        assert payload_hash({"priority": Priority.HIGH}) == payload_hash({"priority": "HIGH"})
        assert payload_hash({"mode": Mode.FAST}) == payload_hash({"mode": 2})

    @pytest.mark.parametrize(
        ("payload", "error"),
        [
            ({"qty": 1.5}, TypeError),
            ({"qty": Decimal("NaN")}, ValueError),
            ({"items": {1, 2}}, TypeError),
            ({"nested": {1: "x"}}, TypeError),
        ],
    )
    def test_ambiguous_values_are_refused(self, payload: dict[str, Any], error: type) -> None:
        with pytest.raises(error):
            payload_hash(payload)


class TestIdempotent:
    def test_a_new_key_executes_and_records_the_result(self, actor: User) -> None:
        command = Command()
        result = command.run(actor, issue_key(actor.pk, OPERATION))
        assert (result.replayed, command.executions) == (False, 1)
        record = IdempotencyRecord.objects.get()
        assert record.actor_id == actor.pk
        assert (record.result_type, record.result_id) == ("core.documentsequence", result.value.pk)
        assert record.payload_hash == payload_hash({"qty": Decimal("1.500")})
        assert str(record).startswith(OPERATION)

    def test_a_replay_returns_the_first_result(self, actor: User) -> None:
        command = Command()
        key = issue_key(actor.pk, OPERATION)
        first = command.run(actor, key)
        again = command.run(actor, key)
        assert again.replayed is True
        assert again.value.pk == first.value.pk
        assert command.executions == 1
        assert command.replays == [("core.documentsequence", first.value.pk)]

    def test_a_replay_is_authorized_again(self, actor: User) -> None:
        command = Command()
        key = issue_key(actor.pk, OPERATION)
        command.run(actor, key)

        def denied(result_type: str, result_id: UUID) -> DocumentSequence:
            raise PermissionError("no longer allowed to view the result")

        command.replay = denied  # type: ignore[method-assign]
        with pytest.raises(PermissionError):
            command.run(actor, key)
        assert command.executions == 1

    def test_the_same_key_with_another_payload_is_a_conflict(self, actor: User) -> None:
        command = Command()
        key = issue_key(actor.pk, OPERATION)
        command.run(actor, key)
        with pytest.raises(IdempotencyConflict) as exc:
            command.run(actor, key, {"qty": Decimal("9.000")})
        assert exc.value.status_code == 409
        assert command.executions == 1
        assert DocumentSequence.objects.count() == 1

    def test_the_same_key_from_another_actor_is_refused(self, actor: User, other: User) -> None:
        command = Command()
        key = issue_key(actor.pk, OPERATION)
        command.run(actor, key)
        with pytest.raises(IdempotencyKeyInvalid):
            command.run(other, key)
        assert command.executions == 1
        assert IdempotencyRecord.objects.count() == 1

    def test_a_tampered_key_writes_nothing(self, actor: User) -> None:
        command = Command()
        with pytest.raises(IdempotencyKeyInvalid):
            command.run(actor, issue_key(actor.pk, OPERATION) + "x")
        assert command.executions == 0
        assert not IdempotencyRecord.objects.exists()

    def test_a_record_without_a_result_is_a_conflict(self, actor: User) -> None:
        key = issue_key(actor.pk, OPERATION)
        IdempotencyRecord.objects.create(
            actor=actor,
            key_id=verify_key(key, actor_id=actor.pk, operation=OPERATION),
            operation=OPERATION,
            payload_hash=payload_hash({"qty": Decimal("1.500")}),
        )
        with pytest.raises(IdempotencyConflict):
            Command().run(actor, key)

    def test_a_failed_command_leaves_no_record(self, actor: User) -> None:
        key = issue_key(actor.pk, OPERATION)

        def fail() -> DocumentSequence:
            raise RuntimeError("validation failed")

        with pytest.raises(RuntimeError), transaction.atomic():
            idempotent(
                actor_id=actor.pk,
                key=key,
                operation=OPERATION,
                payload={},
                execute=fail,
                replay=Command().replay,
            )
        assert not IdempotencyRecord.objects.exists()
        # The key is still usable: the first attempt rolled back entirely.
        assert Command().run(actor, key, {}).replayed is False

    def test_other_integrity_errors_propagate(
        self, actor: User, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def refuse(self: QuerySet[Any], **kwargs: Any) -> Any:
            raise IntegrityError("some other constraint")

        monkeypatch.setattr(QuerySet, "create", refuse)
        command = Command()
        with pytest.raises(IntegrityError, match="other constraint"):
            command.run(actor, issue_key(actor.pk, OPERATION))
        assert command.executions == 0

    @pytest.mark.django_db(transaction=True)
    def test_autocommit_is_refused(self, actor: User) -> None:
        with pytest.raises(IdempotencyOutsideTransaction):
            idempotent(
                actor_id=actor.pk,
                key=issue_key(actor.pk, OPERATION),
                operation=OPERATION,
                payload={},
                execute=Command().execute,
                replay=Command().replay,
            )


class TestExecuteOnce:
    def run(self, command: Command, actor: User, key: str | None) -> DocumentSequence:
        with transaction.atomic():
            return execute_once(
                actor_id=actor.pk,
                key=key,
                operation=OPERATION,
                payload={"qty": Decimal("1.500")},
                execute=command.execute,
                replay=command.replay,
            )

    def test_without_a_key_it_just_executes(self, actor: User) -> None:
        command = Command()
        self.run(command, actor, None)
        self.run(command, actor, None)
        assert command.executions == 2
        assert not IdempotencyRecord.objects.exists()

    def test_with_a_key_it_runs_once(self, actor: User) -> None:
        command = Command()
        key = issue_key(actor.pk, OPERATION)
        first = self.run(command, actor, key)
        assert self.run(command, actor, key) == first
        assert command.executions == 1


class TestPurge:
    def age_records(self, days: float) -> None:
        IdempotencyRecord.objects.update(created_at=datetime.now(UTC) - timedelta(days=days))

    def test_records_younger_than_7_days_are_kept(self, actor: User) -> None:
        Command().run(actor, issue_key(actor.pk, OPERATION))
        self.age_records(6.9)
        assert purge_expired_records() == 0
        assert IdempotencyRecord.objects.count() == 1

    def test_records_older_than_7_days_are_purged(self, actor: User) -> None:
        Command().run(actor, issue_key(actor.pk, OPERATION))
        self.age_records(7.1)
        assert purge_expired_records() == 1
        assert not IdempotencyRecord.objects.exists()

    def test_a_replay_across_the_purge_boundary_is_rejected_as_expired(
        self, actor: User, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A key old enough to have lost its record fails its age check before any lookup."""
        command = Command()
        issued_ago(monkeypatch, 8 * DAY)
        key = issue_key(actor.pk, OPERATION)
        monkeypatch.undo()
        # The first submit happened 8 days ago, when the key was fresh.
        monkeypatch.setattr(idempotency, "_key_max_age", lambda: 9 * DAY)
        command.run(actor, key)
        monkeypatch.undo()
        self.age_records(8)
        assert purge_expired_records() == 1

        with pytest.raises(IdempotencyKeyExpired):
            command.run(actor, key)
        assert command.executions == 1
        assert not IdempotencyRecord.objects.exists()
        assert DocumentSequence.objects.count() == 1

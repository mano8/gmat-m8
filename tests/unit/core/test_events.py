"""In-transaction domain events (apps.core.events)."""

from collections.abc import Iterator

import pytest
from django.db import transaction

from apps.core import events
from apps.core.events import Event, EventOutsideTransaction
from apps.core.models import DocumentSequence

pytestmark = pytest.mark.django_db


@pytest.fixture
def received() -> Iterator[list[Event]]:
    seen: list[Event] = []
    unsubscribe = events.subscribe("thing.happened", seen.append)
    yield seen
    unsubscribe()


def test_subscribers_run_synchronously_in_the_transaction(received: list[Event]) -> None:
    with transaction.atomic():
        event = events.publish("thing.happened", thing_id="t1", qty="1.000")
        assert received == [event]
    assert event.payload == {"thing_id": "t1", "qty": "1.000"}


def test_payload_is_read_only(received: list[Event]) -> None:
    with transaction.atomic():
        event = events.publish("thing.happened", value=1)
    with pytest.raises(TypeError):
        event.payload["value"] = 2  # type: ignore[index]


@pytest.mark.django_db(transaction=True)
def test_publishing_outside_a_transaction_is_refused(received: list[Event]) -> None:
    with pytest.raises(EventOutsideTransaction):
        events.publish("thing.happened")
    assert received == []


def test_a_failing_subscriber_rolls_the_publisher_back() -> None:
    def fail(event: Event) -> None:
        raise RuntimeError("subscriber failed")

    unsubscribe = events.subscribe("thing.failed", fail)
    try:
        with pytest.raises(RuntimeError), transaction.atomic():
            DocumentSequence.objects.create(scope="EVT:rollback")
            events.publish("thing.failed")
    finally:
        unsubscribe()
    assert not DocumentSequence.objects.filter(scope="EVT:rollback").exists()


def test_subscribers_run_in_registration_order_once_each() -> None:
    calls: list[str] = []

    @events.receiver("thing.ordered")
    def first(event: Event) -> None:
        calls.append("first")

    def second(event: Event) -> None:
        calls.append("second")

    undo_second = events.subscribe("thing.ordered", second)
    undo_first = events.subscribe("thing.ordered", first)  # already registered: no duplicate
    try:
        with transaction.atomic():
            events.publish("thing.ordered")
    finally:
        undo_first()
        undo_second()
        undo_second()  # unsubscribing twice is harmless
    assert calls == ["first", "second"]


def test_events_without_subscribers_are_fine() -> None:
    with transaction.atomic():
        assert events.publish("nobody.listens").name == "nobody.listens"


@pytest.mark.parametrize("name", ["", "posted", "Receipt.posted", "receipt.", "receipt posted"])
def test_event_names_are_validated(name: str) -> None:
    with pytest.raises(ValueError, match="Invalid event name"):
        events.subscribe(name, lambda event: None)
    with pytest.raises(ValueError, match="Invalid event name"), transaction.atomic():
        events.publish(name)

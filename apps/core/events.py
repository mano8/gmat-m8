"""In-process domain events (plan §4.10).

Services publish events such as ``receipt.posted`` synchronously, inside their own
transaction, after the write. Subscribers (the inventory module in business phase 2,
notifications) run in that same transaction: a subscriber that raises rolls the whole
operation back, and nothing a subscriber writes survives a rollback of the publisher.
Subscribers must not do network I/O; email goes through the outbox.
"""

import re
from collections import defaultdict
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any

from django.db import DEFAULT_DB_ALIAS, connections

EVENT_NAME = re.compile(r"[a-z][a-z_]*(\.[a-z][a-z_]*)+")


@dataclass(frozen=True)
class Event:
    name: str
    payload: Mapping[str, Any]


Handler = Callable[[Event], None]

_handlers: defaultdict[str, list[Handler]] = defaultdict(list)


class EventOutsideTransaction(RuntimeError):
    """``publish`` was called outside a transaction: subscribers would not be atomic with it."""


def _check_name(name: str) -> None:
    if not EVENT_NAME.fullmatch(name):
        raise ValueError(
            f"Invalid event name {name!r}; expected 'noun.verb', e.g. 'receipt.posted'"
        )


def subscribe(name: str, handler: Handler) -> Callable[[], None]:
    """Register ``handler`` for ``name``; return a function that unregisters it.

    Subscribe from an ``AppConfig.ready`` method. Registering the same handler twice for one
    event has no effect.
    """
    _check_name(name)
    handlers = _handlers[name]
    if handler not in handlers:
        handlers.append(handler)

    def unsubscribe() -> None:
        if handler in handlers:
            handlers.remove(handler)

    return unsubscribe


def receiver(name: str) -> Callable[[Handler], Handler]:
    """Decorator form of ``subscribe``."""

    def register(handler: Handler) -> Handler:
        subscribe(name, handler)
        return handler

    return register


def publish(name: str, /, *, using: str = DEFAULT_DB_ALIAS, **payload: Any) -> Event:
    """Call every subscriber of ``name`` now, in registration order, and return the event.

    The payload is read-only. Pass identifiers and values, not model instances, so a
    subscriber reloads what it needs under its own locks.
    """
    _check_name(name)
    if not connections[using].in_atomic_block:
        raise EventOutsideTransaction(f"Event {name!r} published outside a transaction")
    event = Event(name, MappingProxyType(dict(payload)))
    for handler in tuple(_handlers.get(name, ())):
        handler(event)
    return event

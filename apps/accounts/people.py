"""How the application names a person (ADR-0029).

A person is shown by name. Another person's address is shown only where the maintainer
decided it is needed: to group administrators on the people and invitation screens, and on
the review page, the requester's, to who reviews through a managing role. Every other page,
fragment, picker and mail shows the name, and an account created before names were required
shows ``NO_NAME`` until an administrator names it, never its address.
"""

from types import SimpleNamespace
from typing import Any

NO_NAME = "—"


def name_of(user: Any) -> str:
    """The person's full name, or ``NO_NAME``: never the address."""
    name = str(getattr(user, "full_name", "") or "").strip()
    return name or NO_NAME


def recorded_name(event: Any) -> str:
    """Who an audit event names, on a page: the person's name when the event has a user
    (read with the event as ``actor_name=F("actor__full_name")``), else the label it
    recorded (a system actor's, such as ``system:worker``). The label of a person is their
    address at the time, so only the audit viewer shows it."""
    if event.actor_id is None:
        return str(event.actor_label)
    return name_of(SimpleNamespace(full_name=getattr(event, "actor_name", "")))


def name_and_address(user: Any) -> str:
    """``Name <address>`` (the address alone without a name): only where addresses may be
    shown (ADR-0029)."""
    name, email = name_of(user), str(getattr(user, "email", "") or "")
    return email if name == NO_NAME else f"{name} <{email}>"

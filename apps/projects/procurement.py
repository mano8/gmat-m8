"""What follows a change of a project's procurement branch (plan §3.3, ADR-0010 Q2).

A request keeps the procurement branch it was submitted with, and purchasing checks an
order's branch against it; an order's worksite destination is checked against the
project's current procurement branch. Changing the project's procurement branch would
leave its open requests bought by a branch that can no longer ship to the worksite. The
apps that own such documents register a follower here (they refer to projects, so this
app cannot import them); ``update_project`` calls every follower, under its ``AccessState``
lock, before it saves the change. A follower moves its documents or refuses the change
with a ``ValidationError`` on ``procurement_branch``.
"""

from collections.abc import Callable
from dataclasses import dataclass
from uuid import UUID

from apps.access.principals import Principal
from apps.core.locking import LockOrder
from apps.projects.models import Project


@dataclass(frozen=True)
class ProcurementChange:
    """A project's procurement branch moving from ``old_branch_id`` to ``new_branch_id``."""

    principal: Principal
    project: Project
    old_branch_id: UUID
    new_branch_id: UUID
    locks: LockOrder


type Follower = Callable[[ProcurementChange], None]

_followers: list[Follower] = []


def register_follower(follower: Follower) -> Callable[[], None]:
    """Register ``follower`` (from an ``AppConfig.ready``); return its unregister function."""
    if follower not in _followers:
        _followers.append(follower)

    def unregister() -> None:
        if follower in _followers:
            _followers.remove(follower)

    return unregister


def follow(change: ProcurementChange) -> None:
    """Let every registered follower move its documents (or refuse the change)."""
    for follower in _followers:
        follower(change)

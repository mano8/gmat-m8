"""The allocation component the request services lock and reconcile (plan §4.5, §4.7).

Closing or reopening a line or a request, reducing an approved quantity and changing the
priority or needed-by date change a reconciliation input: they may raise the pending of
other requests through the purchase orders they share (E10). Those services therefore
lock the whole allocation component of their request **before** its lines (L2 for every
request header, L3 for every request line, then L4/L5 for the orders), trim the
allocations of the lines they close, and reconcile once their change is applied. A
reviewer's edit (M7b.3) also asks which lines still have a live allocation (a positive
ceiling on an order that is not cancelled, closed orders included): only a line without
one may change its material in place.

Purchasing owns allocations and refers to request lines, so this app cannot import it: it
registers a ``ComponentLocker`` from its ``AppConfig.ready``. Without one (nothing can be
allocated then) the request's header and lines are locked and nothing is trimmed.
"""

from collections.abc import Callable, Collection
from typing import Protocol
from uuid import UUID

from apps.access.principals import Principal
from apps.core.locking import LockLevel, LockOrder
from apps.material_requests.models import MaterialRequest, MaterialRequestLine

# Trim reasons of the request services' closures (purchasing's ``TrimReason`` values).
LINE_CLOSED = "REQUEST_LINE_CLOSED"
REQUEST_CLOSED = "REQUEST_CLOSED"


class LockedComponent(Protocol):
    def allocated_lines(self, line_ids: Collection[UUID]) -> set[UUID]: ...

    def trim_request_lines(
        self, principal: Principal, line_ids: Collection[UUID], *, reason: str, cause: str
    ) -> None: ...

    def reconcile(self, principal: Principal, cause: str) -> object: ...


type ComponentLocker = Callable[[LockOrder, Collection[UUID]], LockedComponent]

_lockers: list[ComponentLocker] = []


def register_component_locker(locker: ComponentLocker) -> Callable[[], None]:
    """Install purchasing's locker; return a function that removes it again.

    Registering the same locker again is harmless (``AppConfig.ready`` runs again when the
    installed apps are overridden); a second, different locker is refused.
    """
    if _lockers and _lockers[0] is not locker:
        raise ValueError("A component locker is already registered")
    if not _lockers:
        _lockers.append(locker)

    def unregister() -> None:
        if locker in _lockers:
            _lockers.remove(locker)

    return unregister


class _Unallocated:
    """The component of a request when nothing can be allocated: its own rows only."""

    def allocated_lines(self, line_ids: Collection[UUID]) -> set[UUID]:
        return set()

    def trim_request_lines(
        self, principal: Principal, line_ids: Collection[UUID], *, reason: str, cause: str
    ) -> None:
        pass

    def reconcile(self, principal: Principal, cause: str) -> object:
        return []


def lock_component(locks: LockOrder, request_ids: Collection[UUID]) -> LockedComponent:
    """Lock the component of these requests (every line of each); see the module note."""
    if _lockers:
        return _lockers[0](locks, request_ids)
    locks.lock(LockLevel.REQUEST_HEADERS, MaterialRequest, request_ids)
    line_ids = MaterialRequestLine.objects.filter(request_id__in=list(request_ids))
    locks.lock(LockLevel.REQUEST_LINES, MaterialRequestLine, line_ids.values_list("pk", flat=True))
    return _Unallocated()

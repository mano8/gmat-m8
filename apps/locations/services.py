"""Location services: warehouses (plan §3.3, Q3).

Worksite locations are created with their project (``apps.projects.services``). These are
organization changes: same write order as ``apps.organizations.services``.
"""

from typing import Any
from uuid import UUID

from django.core.exceptions import ValidationError
from django.utils.translation import gettext_lazy as _

from apps.access.authorization import ORG_MANAGE, authorize_in_txn
from apps.access.generations import lock_access_state
from apps.access.principals import Principal
from apps.audit.recorder import record
from apps.core import events
from apps.core.changes import apply_changes
from apps.core.idempotency import execute_once
from apps.core.locking import LockMode, LockOrder, transactional
from apps.locations.models import Location
from apps.organizations.models import Branch

LOCATION_FIELDS = frozenset({"name", "address", "is_active"})


def _org_change(principal: Principal) -> None:
    locks = LockOrder()
    lock_access_state(locks, mode=LockMode.UPDATE)
    authorize_in_txn(principal, ORG_MANAGE, locks=locks)


@transactional
def create_warehouse(
    principal: Principal,
    *,
    branch_id: UUID,
    name: str,
    address: str = "",
    idempotency_key: str | None = None,
) -> Location:
    """Add a warehouse to an active branch."""
    _org_change(principal)
    payload = {"branch_id": branch_id, "name": name, "address": address}

    def execute() -> Location:
        branch = Branch.objects.get(pk=branch_id)
        if not branch.is_active:
            raise ValidationError(
                {"branch": ValidationError(_("The branch is inactive."), code="inactive")}
            )
        location = Location(kind=Location.Kind.WAREHOUSE, branch=branch, name=name, address=address)
        location.full_clean()
        location.save()
        record(
            principal.audit_identity,
            "location.created",
            obj=location,
            branch_id=branch.pk,
            data={"kind": location.kind, "name": name, "address": address},
        )
        events.publish("location.created", location_id=location.pk)
        return location

    return execute_once(
        actor_id=principal.user_id,
        key=idempotency_key,
        operation="location.create_warehouse",
        payload=payload,
        execute=execute,
        replay=lambda _type, pk: Location.objects.get(pk=pk),
    )


@transactional
def update_location(principal: Principal, location_id: UUID, **changes: Any) -> Location:
    """Rename, re-address, deactivate or reactivate a location.

    A branch's default warehouse cannot be deactivated; choose another default first.
    """
    _org_change(principal)
    location = Location.objects.get(pk=location_id)
    diff = apply_changes(location, changes, LOCATION_FIELDS)
    if not diff:
        return location
    location.full_clean()
    if (
        diff.get("is_active", {}).get("to") is False
        and Branch.objects.filter(default_warehouse_id=location.pk).exists()
    ):
        raise ValidationError(
            {
                "is_active": ValidationError(
                    _("This is the branch's default warehouse; choose another first."),
                    code="default_warehouse",
                )
            }
        )
    location.save()
    record(
        principal.audit_identity,
        "location.updated",
        obj=location,
        branch_id=location.branch_id,
        data=diff,
    )
    events.publish("location.updated", location_id=location.pk, fields=sorted(diff))
    return location

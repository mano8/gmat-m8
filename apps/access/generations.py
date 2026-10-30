"""L0 locks that serialize scope changes with the writes they affect (plan §3.6, §4.7).

Writes lock ``AccessState`` and the actor's ``User`` row ``FOR SHARE``; changes lock the
rows they change first, before any ``FOR SHARE``, so no transaction ever has to upgrade a
shared lock. The generation counters are bumped by database triggers (``access.0002``,
``accounts.0003``), so every code path that changes a scope-defining attribute bumps them:

- ``AccessState.org_generation``: a company created or its ``is_active`` changed; a branch
  created or its ``is_active`` / ``managed_by`` changed; a project created or its
  ``branch`` / ``procurement_branch`` / ``status`` changed;
- ``User.authz_generation``: a membership or project assignment of the user created or
  changed (validity, revocation), ``employer_company``, ``is_active``, ``is_staff`` or
  ``is_superuser`` changed.

The ``User`` row is locked ``FOR NO KEY UPDATE`` rather than ``FOR UPDATE``: it conflicts
with every ``FOR SHARE`` just the same, but does not block the ``FOR KEY SHARE`` that a
foreign-key check takes, so audit and idempotency rows of that user can still be written.
"""

from collections.abc import Iterable
from uuid import UUID

from django.db import connections

from apps.access.models import ACCESS_STATE_ID, AccessState
from apps.accounts.models import User
from apps.core.locking import LockLevel, LockMode, LockOrder


def lock_access_state(locks: LockOrder, *, mode: LockMode = LockMode.SHARE) -> None:
    """Lock the ``AccessState`` row at L0; changes pass ``LockMode.UPDATE``.

    The row comes from ``access.0001``; it is recreated if a database flush removed it.
    """
    locks.enter(LockLevel.ACCESS)
    with connections[locks.using].cursor() as cursor:
        cursor.execute(
            "INSERT INTO access_accessstate (id, org_generation) VALUES (%s, 0)"
            " ON CONFLICT (id) DO NOTHING",
            [ACCESS_STATE_ID],
        )
    locks.lock(LockLevel.ACCESS, AccessState, [ACCESS_STATE_ID], mode=mode)


def lock_users(
    locks: LockOrder, user_ids: Iterable[UUID], *, mode: LockMode = LockMode.SHARE
) -> list[UUID]:
    """Lock ``User`` rows at L0 in id order; return the ids that exist.

    A change to a user's authority passes ``LockMode.NO_KEY_UPDATE`` (see the module note).
    """
    return locks.lock(LockLevel.ACCESS, User, user_ids, mode=mode)


def org_generation() -> int:
    """The current ``AccessState.org_generation`` (0 before the first scope change)."""
    state = AccessState.objects.filter(pk=ACCESS_STATE_ID).only("org_generation").first()
    return state.org_generation if state is not None else 0

"""L0 lock helpers and the ``AccessState`` singleton (apps.access.generations, models)."""

from collections.abc import Callable
from uuid import UUID

import pytest
from django.contrib import admin
from django.db import IntegrityError, transaction

from apps.access.generations import lock_access_state, lock_users, org_generation
from apps.access.models import ACCESS_STATE_ID, AccessState
from apps.access.roles import (
    BRANCH_ROLES,
    COMPANY_ROLES,
    INSTALLATION_ROLES,
    PRIVILEGED_ROLES,
    Role,
)
from apps.accounts.models import User
from apps.core.admin import ReadOnlyModelAdmin
from apps.core.locking import LockLevel, LockMode, LockOrder, LockOrderViolation

pytestmark = pytest.mark.django_db


def test_the_singleton_row_exists_after_migrate() -> None:
    assert AccessState.objects.filter(pk=ACCESS_STATE_ID).exists()
    assert str(AccessState.objects.get()).startswith("org_generation=")


def test_a_second_row_is_refused() -> None:
    with pytest.raises(IntegrityError), transaction.atomic():
        AccessState.objects.create(id=UUID(int=2))


def test_lock_recreates_a_flushed_row() -> None:
    AccessState.objects.all().delete()
    assert org_generation() == 0
    with transaction.atomic():
        lock_access_state(LockOrder(), mode=LockMode.UPDATE)
    assert AccessState.objects.get().org_generation == 0


def test_locks_are_taken_at_l0(make_user: Callable[..., User]) -> None:
    user = make_user()
    with transaction.atomic():
        locks = LockOrder()
        lock_access_state(locks)
        assert lock_users(locks, [user.pk], mode=LockMode.NO_KEY_UPDATE) == [user.pk]
        assert locks.level is LockLevel.ACCESS


def test_l0_cannot_follow_a_later_level() -> None:
    with transaction.atomic():
        locks = LockOrder()
        locks.enter(LockLevel.IDEMPOTENCY)
        with pytest.raises(LockOrderViolation):
            lock_access_state(locks)


def test_every_role_has_exactly_one_shape() -> None:
    shapes = (INSTALLATION_ROLES, BRANCH_ROLES, COMPANY_ROLES)
    assert set().union(*shapes) == set(Role)
    assert sum(len(shape) for shape in shapes) == len(Role)
    assert {Role.GROUP_ADMIN, Role.MATERIAL_MANAGER} == PRIVILEGED_ROLES


def test_access_state_admin_is_read_only() -> None:
    assert isinstance(admin.site._registry[AccessState], ReadOnlyModelAdmin)

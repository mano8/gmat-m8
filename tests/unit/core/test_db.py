import pytest
from django.db import OperationalError, connection

from apps.core.db import atomic_with_lock_timeout, db_now

pytestmark = pytest.mark.django_db


def test_sets_a_local_lock_timeout() -> None:
    with atomic_with_lock_timeout(1234), connection.cursor() as cursor:
        cursor.execute("SHOW lock_timeout")
        row = cursor.fetchone()
    assert row is not None and row[0] in {"1234ms", "1234"}


def test_other_operational_errors_pass_through() -> None:
    with pytest.raises(OperationalError, match="unrelated"), atomic_with_lock_timeout():
        raise OperationalError("unrelated")


def test_db_now_is_the_transaction_start() -> None:
    with atomic_with_lock_timeout():
        first = db_now()
        assert db_now() == first
    assert first.tzinfo is not None

"""The audit table is append-only: UPDATE and DELETE raise, whatever path tries them."""

import pytest
from django.db import IntegrityError, connection, transaction

from apps.audit.models import AuditEvent
from apps.audit.recorder import AuditIdentity, record

pytestmark = [pytest.mark.integration, pytest.mark.django_db]


@pytest.fixture
def event() -> AuditEvent:
    with transaction.atomic():
        return record(AuditIdentity(label="system:worker"), "outbox.purged")


def test_orm_update_is_refused(event: AuditEvent) -> None:
    with pytest.raises(IntegrityError, match="append-only: UPDATE"), transaction.atomic():
        AuditEvent.objects.filter(pk=event.pk).update(action="outbox.forged")


def test_save_of_a_changed_row_is_refused(event: AuditEvent) -> None:
    event.actor_label = "someone else"
    with pytest.raises(IntegrityError, match="append-only"), transaction.atomic():
        event.save()


def test_orm_delete_is_refused(event: AuditEvent) -> None:
    with pytest.raises(IntegrityError, match="append-only: DELETE"), transaction.atomic():
        AuditEvent.objects.filter(pk=event.pk).delete()


def test_sql_update_and_delete_are_refused(event: AuditEvent) -> None:
    for statement in (
        "UPDATE audit_auditevent SET data = '{}'::jsonb",
        "DELETE FROM audit_auditevent",
    ):
        with pytest.raises(IntegrityError), transaction.atomic(), connection.cursor() as cursor:
            cursor.execute(statement)
    assert AuditEvent.objects.get(pk=event.pk).action == "outbox.purged"


def test_inserts_are_allowed(event: AuditEvent) -> None:
    with transaction.atomic():
        record(AuditIdentity(label="system:worker"), "outbox.purged")
    assert AuditEvent.objects.count() == 2

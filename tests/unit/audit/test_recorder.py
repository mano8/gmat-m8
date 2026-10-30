"""Audit events (apps.audit): ``record()`` and the read-only admin."""

import uuid
from collections.abc import Callable
from decimal import Decimal

import pytest
from django.contrib import admin
from django.db import transaction

from apps.accounts.models import User
from apps.audit.models import AuditEvent
from apps.audit.recorder import AuditIdentity, AuditOutsideTransaction, record
from apps.core.admin import ReadOnlyModelAdmin
from apps.core.models import DocumentSequence
from config.logging import request_id_var

pytestmark = pytest.mark.django_db


def test_records_who_did_what_to_which_object(make_user: Callable[..., User]) -> None:
    user = make_user("auditor@example.org")
    target = DocumentSequence.objects.create(scope="AUD:1")
    branch_id = uuid.uuid7()
    token = request_id_var.set("req-123")
    try:
        with transaction.atomic():
            event = record(
                AuditIdentity(label=user.email, user_id=user.pk, via="cli:import", ip="10.0.0.1"),
                "sequence.created",
                obj=target,
                branch_id=branch_id,
                data={"qty": Decimal("1.500")},
            )
    finally:
        request_id_var.reset(token)
    event.refresh_from_db()
    assert event.actor == user
    assert (event.actor_label, event.via, event.ip) == (user.email, "cli:import", "10.0.0.1")
    assert (event.object_type, event.object_id) == ("core.documentsequence", target.pk)
    assert event.branch_id == branch_id
    assert event.data == {"qty": "1.500"}
    assert event.request_id == "req-123"
    assert event.occurred_at is not None
    assert str(event) == "sequence.created by auditor@example.org"


def test_system_actor_without_object() -> None:
    with transaction.atomic():
        event = record(AuditIdentity(label="system:worker"), "outbox.purged")
    event.refresh_from_db()
    assert event.actor is None
    assert (event.object_type, event.object_id, event.data) == ("", None, {})
    assert event.request_id == ""


def test_rolls_back_with_the_change() -> None:
    with pytest.raises(RuntimeError), transaction.atomic():
        record(AuditIdentity(label="system:worker"), "outbox.purged")
        raise RuntimeError("the change failed")
    assert not AuditEvent.objects.exists()


@pytest.mark.django_db(transaction=True)
def test_autocommit_is_refused() -> None:
    with pytest.raises(AuditOutsideTransaction):
        record(AuditIdentity(label="system:worker"), "outbox.purged")


@pytest.mark.parametrize("action", ["", "purged", "Outbox.purged", "outbox purged"])
def test_action_names_are_validated(action: str) -> None:
    with pytest.raises(ValueError, match="action"), transaction.atomic():
        record(AuditIdentity(label="system:worker"), action)


def test_an_identity_needs_a_label() -> None:
    with pytest.raises(ValueError, match="label"), transaction.atomic():
        record(AuditIdentity(label=""), "outbox.purged")


def test_the_admin_is_read_only() -> None:
    assert isinstance(admin.site._registry[AuditEvent], ReadOnlyModelAdmin)

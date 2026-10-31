"""The outbox's database rules: shape per kind, frozen content, own attempts (plan §4.2)."""

import uuid
from collections.abc import Callable
from typing import Any

import pytest
from django.db import IntegrityError, connection, transaction
from django.db.models.functions import Now
from django.utils import timezone

from apps.accounts.models import User
from apps.notifications.eligibility import ACCOUNT_ACTIVE
from apps.notifications.models import MAX_RECIPIENTS, EmailAttempt, EmailOutbox, Kind, Status
from apps.notifications.outbox import queue_security_email

pytestmark = [pytest.mark.integration, pytest.mark.django_db]


@pytest.fixture
def user(make_user: Callable[..., User]) -> User:
    return make_user("frozen@example.org")


@pytest.fixture
def message(user: User) -> EmailOutbox:
    return queue_security_email(
        address=user.email, target=user, rule=ACCOUNT_ACTIVE, subject="Reset", body="secret"
    )


def refused(**changes: Any) -> Callable[[EmailOutbox], None]:
    def apply(message: EmailOutbox) -> None:
        with pytest.raises(IntegrityError), transaction.atomic():
            EmailOutbox.objects.filter(pk=message.pk).update(**changes)

    return apply


def row(**values: Any) -> dict[str, Any]:
    base: dict[str, Any] = {"from_email": "f@example.org", "subject": "s", "body": "b"}
    return base | values


class TestShapePerKind:
    @pytest.mark.parametrize(
        "values",
        [
            {"kind": Kind.NOTIFICATION, "recipient_users": [], "eligibility_rule": "project.view"},
            {
                "kind": Kind.NOTIFICATION,
                "recipient_users": [uuid.uuid4() for _ in range(MAX_RECIPIENTS + 1)],
                "eligibility_rule": "project.view",
            },
            {"kind": Kind.NOTIFICATION, "recipient_users": [uuid.uuid4()]},  # no rule
            {
                "kind": Kind.NOTIFICATION,
                "recipient_users": [uuid.uuid4()],
                "eligibility_rule": "project.view",
                "recipient_address": "x@example.org",
            },
            {
                "kind": Kind.NOTIFICATION,
                "recipient_users": [uuid.uuid4()],
                "eligibility_rule": "project.view",
                "sensitive": True,
            },
            # A security email needs its address, rule, target and expiry, and is sensitive.
            {"kind": Kind.SECURITY, "sensitive": True, "recipient_address": "x@example.org"},
            {
                "kind": Kind.SECURITY,
                "recipient_address": "x@example.org",
                "eligibility_rule": ACCOUNT_ACTIVE,
                "eligibility_object_type": "accounts.user",
                "eligibility_object_id": uuid.uuid4(),
                "expires_at": timezone.now(),
            },
            {"kind": Kind.PO_ORDER, "recipient_address": "x@example.org"},  # no order
            {"kind": Kind.PO_ORDER, "source_po": uuid.uuid4()},  # no address
            {
                "kind": Kind.PO_ORDER,
                "source_po": uuid.uuid4(),
                "recipient_address": "x@example.org",
                "eligibility_object_type": "projects.project",  # half a reference
            },
        ],
    )
    def test_a_malformed_message_is_refused(self, values: dict[str, Any]) -> None:
        with pytest.raises(IntegrityError), transaction.atomic():
            EmailOutbox.objects.create(**row(**values))

    def test_one_cancellation_notice_per_order(self) -> None:
        order = uuid.uuid4()
        values = row(kind=Kind.PO_CANCELLATION, source_po=order, recipient_address="s@e.org")
        EmailOutbox.objects.create(**values)
        EmailOutbox.objects.create(
            **row(kind=Kind.PO_ORDER, source_po=order, recipient_address="s@e.org")
        )
        with pytest.raises(IntegrityError), transaction.atomic():
            EmailOutbox.objects.create(**values)


class TestFrozenContent:
    @pytest.mark.parametrize(
        "change",
        [
            refused(subject="Changed"),
            refused(body="other"),
            refused(html_body="<p>other</p>"),
            refused(recipient_address="attacker@example.org"),
            refused(eligibility_rule="other.rule"),
            refused(eligibility_object_id=uuid.uuid4()),
            refused(kind=Kind.PO_ORDER),
            refused(sensitive=False),
            refused(from_email="other@example.org"),
            refused(source_ref="other"),
            refused(expires_at=Now()),
            refused(created_at=Now()),
        ],
    )
    def test_what_a_message_says_and_to_whom_never_changes(
        self, message: EmailOutbox, change: Callable[[EmailOutbox], None]
    ) -> None:
        change(message)

    def test_the_worker_may_move_it_along_and_wipe_it(self, message: EmailOutbox) -> None:
        EmailOutbox.objects.filter(pk=message.pk).update(
            status=Status.FAILED, attempts=1, last_error="x", not_before=Now(), message_id="<1@x>"
        )
        EmailOutbox.objects.filter(pk=message.pk).update(body="", html_body="", wiped_at=Now())
        refused(message_id="<2@x>")(message)
        refused(wiped_at=None)(message)
        message.refresh_from_db()
        assert (message.status, message.body, message.message_id) == (Status.FAILED, "", "<1@x>")

    def test_the_rule_holds_for_any_database_client(self, message: EmailOutbox) -> None:
        with pytest.raises(IntegrityError), transaction.atomic(), connection.cursor() as cursor:
            cursor.execute(
                "UPDATE notifications_emailoutbox SET subject = 'x' WHERE id = %s", [message.pk]
            )


class TestAttempts:
    def test_sending_needs_an_attempt(self, message: EmailOutbox) -> None:
        refused(status=Status.SENDING)(message)

    def test_a_message_points_only_at_its_own_attempts(
        self, message: EmailOutbox, user: User
    ) -> None:
        other = queue_security_email(
            address=user.email, target=user, rule=ACCOUNT_ACTIVE, subject="s", body="b"
        )
        foreign = EmailAttempt.objects.create(message=other, number=1)
        refused(current_attempt=foreign)(message)
        own = EmailAttempt.objects.create(message=message, number=1)
        EmailOutbox.objects.filter(pk=message.pk).update(current_attempt=own, status=Status.SENDING)

    @pytest.mark.parametrize(
        "values",
        [
            {"number": 0},
            {"number": 1, "outcome": "SENT"},  # an outcome needs its finish time
            {"number": 1, "finished_at": timezone.now()},
        ],
    )
    def test_attempt_rules(self, message: EmailOutbox, values: dict[str, Any]) -> None:
        with pytest.raises(IntegrityError), transaction.atomic():
            EmailAttempt.objects.create(message=message, **values)

    def test_attempt_numbers_are_unique(self, message: EmailOutbox) -> None:
        EmailAttempt.objects.create(message=message, number=1)
        with pytest.raises(IntegrityError), transaction.atomic():
            EmailAttempt.objects.create(message=message, number=1)

    def test_str(self, message: EmailOutbox) -> None:
        attempt = EmailAttempt.objects.create(message=message, number=1)
        assert str(message) == f"SECURITY {message.pk} (QUEUED)"
        assert str(attempt) == f"attempt 1 of {message.pk}"

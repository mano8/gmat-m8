"""Queueing: frozen content, sealed sensitive bodies, the outbox mailer (plan §5)."""

from collections.abc import Callable
from datetime import timedelta
from typing import Any

import pytest
from cryptography.fernet import Fernet, InvalidToken
from django.core import mail
from django.core.exceptions import ImproperlyConfigured
from django.core.mail import EmailMessage, EmailMultiAlternatives, send_mail
from django.db import connection, transaction
from django.test import override_settings

from apps.access.roles import Capability
from apps.accounts.models import User
from apps.notifications import crypto
from apps.notifications.backends import OutboxEmailBackend, UnsupportedMessage
from apps.notifications.eligibility import ACCOUNT_ACTIVE
from apps.notifications.models import MAX_RECIPIENTS, EmailOutbox, Kind, Status
from apps.notifications.outbox import (
    OutboxOutsideTransaction,
    queue_dispatch,
    queue_notification,
)
from tests.org import Org
from tests.outbox import SECRET_BODY

pytestmark = pytest.mark.django_db


def raw_columns(message: EmailOutbox) -> tuple[str, str]:
    """``body`` and ``html_body`` as stored in the table, bypassing the model."""
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT body, html_body FROM notifications_emailoutbox WHERE id = %s", [message.pk]
        )
        body, html_body = cursor.fetchone()
    return body, html_body


class TestCrypto:
    def test_round_trip_and_empty(self) -> None:
        sealed = crypto.seal("secret")
        assert sealed != "secret"
        assert crypto.unseal(sealed) == "secret"
        assert crypto.seal("") == crypto.unseal("") == ""

    def test_rotation_keeps_old_bodies_readable(self) -> None:
        old, new = Fernet.generate_key().decode(), Fernet.generate_key().decode()
        with override_settings(OUTBOX_ENCRYPTION_KEYS=[old]):
            sealed = crypto.seal("secret")
        with override_settings(OUTBOX_ENCRYPTION_KEYS=[new, old]):
            assert crypto.unseal(sealed) == "secret"
        with override_settings(OUTBOX_ENCRYPTION_KEYS=[new]), pytest.raises(InvalidToken):
            crypto.unseal(sealed)

    def test_no_key_is_a_configuration_error(self) -> None:
        with override_settings(OUTBOX_ENCRYPTION_KEYS=[]), pytest.raises(ImproperlyConfigured):
            crypto.seal("secret")


class TestSecurityEmail:
    def test_body_is_sealed_at_rest_and_expires_in_an_hour(
        self, security_mail: Callable[..., EmailOutbox]
    ) -> None:
        message = security_mail(html_body="<p>link</p>")
        body, html_body = raw_columns(message)
        assert "SECRETTOKEN" not in body
        assert "link" not in html_body
        assert crypto.unseal(body) == SECRET_BODY
        message.refresh_from_db()
        assert message.kind == Kind.SECURITY
        assert message.sensitive is True
        assert message.status == Status.QUEUED
        assert message.expires_at is not None
        assert timedelta(minutes=59) < message.expires_at - message.not_before <= timedelta(hours=1)

    def test_an_unknown_rule_is_refused(self, security_mail: Callable[..., EmailOutbox]) -> None:
        with pytest.raises(ValueError, match="Unknown security rule"):
            security_mail(rule="nobody.registered.this")


class TestNotification:
    def test_recipients_are_deduplicated_and_split_by_twenty(
        self, org: Org, make_user: Callable[..., User]
    ) -> None:
        users = [make_user(f"u{index}@example.org") for index in range(MAX_RECIPIENTS + 3)]
        ids = [user.pk for user in users]
        with transaction.atomic():
            messages = queue_notification(
                recipients=[*ids, ids[0]],
                capability=Capability.PROJECT_VIEW,
                obj=org.hospital,
                subject="s",
                body="b",
            )
        assert [len(message.recipient_users) for message in messages] == [MAX_RECIPIENTS, 3]
        assert sorted(user for m in messages for user in m.recipient_users) == sorted(ids)
        first = messages[0]
        assert first.eligibility_object_type == "projects.project"
        assert first.eligibility_object_id == org.hospital.pk
        assert first.sensitive is False

    def test_nobody_to_tell_queues_nothing(self, org: Org) -> None:
        with transaction.atomic():
            assert (
                queue_notification(
                    recipients=[],
                    capability=Capability.PROJECT_VIEW,
                    obj=None,
                    subject="s",
                    body="b",
                )
                == []
            )

    @pytest.mark.parametrize(
        ("kwargs", "error"),
        [
            ({"capability": "outbox.send"}, ValueError),
            ({"capability": "no.such"}, ValueError),
            ({"obj": "user"}, TypeError),
        ],
    )
    def test_the_rule_must_be_checkable(
        self, account: User, kwargs: dict[str, Any], error: type[Exception]
    ) -> None:
        values: dict[str, Any] = {
            "recipients": [account.pk],
            "capability": Capability.PROJECT_VIEW,
            "obj": None,
            "subject": "s",
            "body": "b",
        }
        if kwargs.get("obj") == "user":
            kwargs = {"obj": account}  # users have no scoped queryset
        with transaction.atomic(), pytest.raises(error):
            queue_notification(**(values | kwargs))

    @pytest.mark.django_db(transaction=True)
    def test_business_mail_is_queued_inside_the_business_transaction(
        self, account: User, org: Org
    ) -> None:
        with pytest.raises(OutboxOutsideTransaction):
            queue_notification(
                recipients=[account.pk],
                capability=Capability.PROJECT_VIEW,
                obj=org.hospital,
                subject="s",
                body="b",
            )
        with pytest.raises(OutboxOutsideTransaction):
            queue_dispatch(
                kind=Kind.PO_ORDER, po_id=org.hospital.pk, address="a@b.c", subject="s", body="b"
            )

    def test_a_rolled_back_change_sends_nothing(self, account: User, org: Org) -> None:
        with pytest.raises(RuntimeError), transaction.atomic():
            queue_notification(
                recipients=[account.pk],
                capability=Capability.PROJECT_VIEW,
                obj=org.hospital,
                subject="s",
                body="b",
            )
            raise RuntimeError("the business change failed")
        assert not EmailOutbox.objects.exists()


class TestDispatch:
    def test_only_dispatch_kinds(self, org: Org) -> None:
        with transaction.atomic(), pytest.raises(ValueError, match="not a dispatch kind"):
            queue_dispatch(
                kind=Kind.SECURITY, po_id=org.hospital.pk, address="a@b.c", subject="s", body="b"
            )

    def test_queued_with_its_order(self, dispatch: Callable[..., EmailOutbox], org: Org) -> None:
        message = dispatch()
        assert message.source_po == org.hospital.pk
        assert message.is_dispatch
        assert message.sensitive is False


class TestOutboxMailer:
    """``MAILERS["default"]``: Django's and allauth's mail lands in the outbox."""

    def test_send_mail_queues_a_sealed_security_email(self, account: User) -> None:
        assert send_mail("Reset", SECRET_BODY, None, [account.email]) == 1
        assert mail.outbox == []
        message = EmailOutbox.objects.get()
        assert message.kind == Kind.SECURITY
        assert message.recipient_address == account.email
        assert message.eligibility_rule == ACCOUNT_ACTIVE
        assert message.eligibility_object_id == account.pk
        assert message.from_email == "gmat-m8 <no-reply@localhost>"
        assert message.source_ref == "django-mail"
        assert crypto.unseal(message.body) == SECRET_BODY

    def test_html_bodies_are_kept(self, account: User) -> None:
        both = EmailMultiAlternatives("s", "text", to=[account.email])
        both.attach_alternative("<p>html</p>", "text/html")
        html_only = EmailMessage("s", "<p>only</p>", to=[account.email])
        html_only.content_subtype = "html"
        assert OutboxEmailBackend().send_messages([both, html_only]) == 2
        first, second = EmailOutbox.objects.order_by("created_at", "id")
        assert (crypto.unseal(first.body), crypto.unseal(first.html_body)) == (
            "text",
            "<p>html</p>",
        )
        assert (crypto.unseal(second.body), crypto.unseal(second.html_body)) == ("", "<p>only</p>")

    def test_an_address_without_account_is_dropped(self, caplog: pytest.LogCaptureFixture) -> None:
        assert send_mail("s", "b", None, ["stranger@example.org"]) == 0
        assert not EmailOutbox.objects.exists()
        assert "stranger" not in caplog.text

    def test_nothing_to_send(self) -> None:
        assert OutboxEmailBackend().send_messages([]) == 0

    @pytest.mark.parametrize(
        "change",
        [
            lambda m: setattr(m, "to", ["a@example.org", "b@example.org"]),
            lambda m: setattr(m, "cc", ["b@example.org"]),
            lambda m: setattr(m, "to", ["not-an-address"]),
            lambda m: m.attach("x.txt", "x", "text/plain"),
            lambda m: setattr(m, "extra_headers", {"X-Thing": "1"}),
            lambda m: setattr(m, "reply_to", ["c@example.org"]),
            lambda m: m.attach_alternative("x", "text/calendar"),
            lambda m: [m.attach_alternative("x", "text/html") for _ in range(2)],
        ],
    )
    def test_what_the_outbox_cannot_keep_is_refused(self, account: User, change: Any) -> None:
        message = EmailMultiAlternatives("s", "b", to=[account.email])
        change(message)
        with pytest.raises(UnsupportedMessage):
            OutboxEmailBackend().send_messages([message])
        assert not EmailOutbox.objects.exists()

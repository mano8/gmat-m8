"""Delivery status of invitation and email-change mail (M7c.1, Q37-Q38)."""

import re
import uuid
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
from django.conf import settings
from django.db import connection
from django.test import override_settings
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone, translation
from django.utils.translation import gettext

from apps.access.context import AccessContext
from apps.access.principals import SystemPrincipal
from apps.access.roles import Capability, Role
from apps.accounts.models import EmailChangeRequest, Invitation, User
from apps.core.idempotency import issue_key
from apps.notifications import selectors
from apps.notifications.eligibility import ACCOUNT_ACTIVE
from apps.notifications.models import EmailAttempt, EmailOutbox, Status
from apps.notifications.outbox import queue_security_email
from apps.notifications.worker import PAUSE_FLAG, Worker, send
from tests.org import Org, People, client_for
from tests.smtp import fake_smtp, smtp_mailers
from tests.tokens import last_token

pytestmark = pytest.mark.django_db

PENDING = {Status.QUEUED, Status.SENDING}


def invite(client: Any, org: Org, people: People, email: str = "newbie@example.org") -> Invitation:
    response = client.post(
        reverse("org:create_invitation"),
        {
            "email": email,
            "role": Role.TECHNICIAN,
            "branch": org.a_mad.pk,
            "idempotency_key": issue_key(people.group_admin.pk, "invitation.create"),
        },
    )
    assert response.status_code == 302
    return Invitation.objects.get(email=email)


def outbox_of(target: Invitation | EmailChangeRequest) -> EmailOutbox:
    return EmailOutbox.objects.get(eligibility_object_id=target.pk)


def set_status(message: EmailOutbox, status: str) -> None:
    """Move the row the way the worker does (``SENDING`` needs its current attempt)."""
    values: dict[str, Any] = {"status": status}
    if status == Status.SENDING:
        attempt = EmailAttempt.objects.create(message=message, number=1)
        values["current_attempt"] = attempt
    if status == Status.SENT:
        values["sent_at"] = timezone.now()
    EmailOutbox.objects.filter(pk=message.pk).update(**values)


def badge(response: Any) -> str:
    html = response.content.decode()
    found = re.search(
        r'<span id="delivery-[^"]+".*?data-testid="delivery-status".*?</span>\s*</span>', html, re.S
    )
    assert found, html
    return found.group(0)


def t(text: Any) -> str:
    """``text`` in the language the pages render in (never the thread's leftover one)."""
    with translation.override(settings.LANGUAGE_CODE):
        return gettext(str(text)) if isinstance(text, str) else str(text)


def ctx_of(user: User) -> AccessContext:
    return AccessContext.for_user(user)


@pytest.fixture
def admin_client(people: People) -> Any:
    return client_for(people.group_admin)


class TestBadge:
    @pytest.mark.parametrize("status", list(Status))
    def test_every_state_is_a_chip_and_a_word(
        self, org: Org, people: People, admin_client: Any, status: Status
    ) -> None:
        invitation = invite(admin_client, org, people)
        set_status(outbox_of(invitation), status)
        response = admin_client.get(reverse("org:invitation_delivery", args=[invitation.pk]))
        assert response.status_code == 200
        html = response.content.decode()
        assert t(status.label) in html
        assert f'data-state="{status.value.lower()}"' in html
        polls = f'hx-get="{reverse("org:invitation_delivery", args=[invitation.pk])}"' in html
        assert polls is (status in PENDING)

    def test_a_sent_message_shows_when(self, org: Org, people: People, admin_client: Any) -> None:
        invitation = invite(admin_client, org, people)
        message = outbox_of(invitation)
        set_status(message, Status.SENT)
        message.refresh_from_db()
        shown = admin_client.get(reverse("org:invitation_delivery", args=[invitation.pk]))
        stamp = message.sent_at.astimezone().strftime("%H:%M") if message.sent_at else ""
        assert t(Status.SENT.label) in shown.content.decode()
        assert stamp in shown.content.decode()

    def test_the_list_and_the_person_page_carry_the_badge_and_the_flash_says_queued(
        self, org: Org, people: People, admin_client: Any
    ) -> None:
        client = admin_client
        form = client.get(reverse("org:new_invitation")).content.decode()
        key = re.search(r'name="idempotency_key" value="([^"]+)"', form)
        assert key is not None
        created = client.post(
            reverse("org:create_invitation"),
            {
                "email": "newbie@example.org",
                "role": Role.TECHNICIAN,
                "branch": org.a_mad.pk,
                "idempotency_key": key.group(1),
            },
            follow=True,
        )
        flash = [str(message) for message in created.context["messages"]]
        assert flash == [
            t("Invitation queued for sending to %(email)s.") % {"email": "newbie@example.org"}
        ]
        assert 'data-testid="delivery-status"' in created.content.decode()
        invitation = Invitation.objects.get()
        again = client.post(reverse("org:resend_invitation", args=[invitation.pk]), follow=True)
        assert [str(message) for message in again.context["messages"]][-1] == t(
            "Invitation queued for sending again; the previous link no longer works."
        )
        assert "sent again" not in again.content.decode().lower()
        newer = Invitation.objects.get(revoked_at__isnull=True)
        assert outbox_of(invitation).status == Status.EXPIRED  # the old link is gone
        listed = client.get(reverse("org:invitations")).content.decode()
        assert listed.count('data-testid="delivery-status"') == 1
        assert reverse("org:invitation_delivery", args=[newer.pk]) in listed
        partial = client.get(reverse("org:invitations"), HTTP_HX_REQUEST="true").content.decode()
        assert 'data-testid="delivery-status"' in partial

    def test_a_revoked_invitation_has_no_badge_and_its_fragment_is_empty(
        self, org: Org, people: People, admin_client: Any
    ) -> None:
        invitation = invite(admin_client, org, people)
        Invitation.objects.filter(pk=invitation.pk).update(revoked_at=timezone.now())
        fragment = admin_client.get(reverse("org:invitation_delivery", args=[invitation.pk]))
        assert fragment.status_code == 200
        assert 'data-testid="delivery-status"' not in fragment.content.decode()  # the poll ends

    def test_polling_stops_by_itself(self, org: Org, people: People, admin_client: Any) -> None:
        invitation = invite(admin_client, org, people)
        url = reverse("org:invitation_delivery", args=[invitation.pk])
        assert "every 5s" in admin_client.get(url).content.decode()
        set_status(outbox_of(invitation), Status.SENT)
        assert "hx-trigger" not in admin_client.get(url).content.decode()


class TestDelayed:
    def test_a_message_still_queued_after_ten_minutes_is_delayed(
        self,
        org: Org,
        people: People,
        admin_client: Any,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        invitation = invite(admin_client, org, people)
        url = reverse("org:invitation_delivery", args=[invitation.pk])
        created = outbox_of(invitation).created_at
        monkeypatch.setattr(selectors, "now", lambda: created + timedelta(minutes=9, seconds=59))
        assert 'data-state="queued"' in admin_client.get(url).content.decode()
        monkeypatch.setattr(selectors, "now", lambda: created + timedelta(minutes=10, seconds=1))
        html = admin_client.get(url).content.decode()
        assert 'data-state="delayed"' in html
        assert t("Still not sent. Tell the operator.") in html
        assert "every 5s" in html  # it still follows the message: the worker may resume

    def test_a_paused_worker_leaves_it_queued_then_delayed(
        self,
        org: Org,
        people: People,
        admin_client: Any,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        invitation = invite(admin_client, org, people)
        monkeypatch.setattr("apps.notifications.worker.connections.close_all", lambda: None)
        flags = tmp_path / "flags"
        flags.mkdir()
        (flags / PAUSE_FLAG).touch()
        worker = Worker(
            principal=SystemPrincipal(),
            flags_dir=flags,
            heartbeat_file=tmp_path / "heartbeat",
            outbound=True,
            poll_interval=0.01,
            reconcile_interval=60,
            housekeeping_interval=3600,
            name="test-worker",
        )
        worker.tick()
        created = outbox_of(invitation).created_at
        monkeypatch.setattr(selectors, "now", lambda: created + timedelta(minutes=11))
        shown = admin_client.get(reverse("org:invitation_delivery", args=[invitation.pk]))
        assert 'data-state="delayed"' in shown.content.decode()


class TestSentByTheWorker:
    def test_an_invitation_sent_to_the_smtp_server_shows_sent_with_its_time(
        self, org: Org, people: People, admin_client: Any, tmp_path: Path
    ) -> None:
        invitation = invite(admin_client, org, people)
        url = reverse("org:invitation_delivery", args=[invitation.pk])
        assert 'data-state="queued"' in admin_client.get(url).content.decode()
        flags = tmp_path / "flags"
        flags.mkdir()
        with fake_smtp() as server, override_settings(MAILERS=smtp_mailers(server.port)):
            worker = Worker(
                principal=SystemPrincipal(),
                flags_dir=flags,
                heartbeat_file=tmp_path / "heartbeat",
                outbound=True,
                poll_interval=0.01,
                reconcile_interval=60,
                housekeeping_interval=3600,
                name="test-worker",
                send=send,
            )
            assert worker.tick() is True
            assert len(server.received) == 1
        message = outbox_of(invitation)
        assert message.status == Status.SENT and message.sent_at is not None
        html = admin_client.get(url).content.decode()
        assert 'data-state="sent"' in html and "hx-trigger" not in html
        assert message.sent_at.astimezone().strftime("%H:%M") in html


class TestEmailChange:
    def change(self, people: People, client: Any) -> EmailChangeRequest:
        response = client.post(
            reverse("org:request_email_change", args=[people.tech_mad.pk]),
            {"new_email": "fresh@example.org"},
            follow=True,
        )
        assert [str(m) for m in response.context["messages"]][-1] == (
            t("Confirmation queued for sending to the new address; the current one can cancel.")
        )
        return EmailChangeRequest.objects.get(user=people.tech_mad)

    def test_the_person_page_shows_both_messages_and_the_fragment_follows_them(
        self, people: People, admin_client: Any
    ) -> None:
        change = self.change(people, admin_client)
        page = admin_client.get(reverse("org:person", args=[people.tech_mad.pk]))
        html = page.content.decode()
        assert html.count('data-testid="delivery-status"') == 2
        assert t("Confirmation to the new address") in html
        assert t("Notice to the current address") in html
        url = reverse("org:email_change_delivery", args=[change.pk])
        assert url in html
        confirm, notice = EmailOutbox.objects.filter(eligibility_object_id=change.pk).order_by(
            "source_ref"
        )[::-1]
        assert (confirm.source_ref, notice.source_ref) == (
            "email_change.confirm",
            "email_change.cancel",
        )
        set_status(confirm, Status.SENT)
        set_status(notice, Status.FAILED)
        fragment = admin_client.get(url).content.decode()
        assert 'data-state="sent"' in fragment and 'data-state="failed"' in fragment
        assert "hx-trigger" not in fragment
        set_status(notice, Status.QUEUED)
        assert "every 5s" in admin_client.get(url).content.decode()


class TestWhatIsShown:
    def test_never_the_token_the_body_the_error_or_the_address_of_others(
        self, org: Org, people: People, admin_client: Any
    ) -> None:
        invitation = invite(admin_client, org, people)
        token = last_token("newbie@example.org")
        EmailOutbox.objects.filter(pk=outbox_of(invitation).pk).update(
            status=Status.FAILED, last_error="SMTP 550 at RCPT SENTINEL-ERROR"
        )
        for url in (
            reverse("org:invitation_delivery", args=[invitation.pk]),
            reverse("org:invitations"),
            reverse("org:person", args=[people.group_admin.pk]),
        ):
            html = admin_client.get(url).content.decode()
            assert "SENTINEL-ERROR" not in html and "RCPT" not in html
            assert token not in html
        fragment = admin_client.get(reverse("org:invitation_delivery", args=[invitation.pk]))
        assert "newbie@example.org" not in fragment.content.decode()

    def test_the_selector_reads_four_columns_and_only_for_group_administrators(
        self, org: Org, people: People
    ) -> None:
        ctx = ctx_of(people.group_admin)
        found = invite(client_for(people.group_admin), org, people)
        query = selectors.delivery_status(ctx, selectors.INVITATION, [found.pk])
        assert set(query) == {found.pk}
        mm = ctx_of(people.mm_mad)
        assert selectors.delivery_status(mm, selectors.INVITATION, [found.pk]) == {}
        assert selectors.delivery_status(ctx, "accounts.user", [found.pk]) == {}
        assert selectors.delivery_status(ctx, selectors.INVITATION, []) == {}
        with CaptureQueriesContext(connection) as queries:
            selectors.delivery_status(ctx, selectors.INVITATION, [found.pk])
        assert "body" not in queries[-1]["sql"] and "last_error" not in queries[-1]["sql"]
        assert "recipient_address" not in queries[-1]["sql"]

    def test_the_latest_message_wins_and_one_query_serves_a_list(
        self, org: Org, people: People
    ) -> None:
        ctx = ctx_of(people.group_admin)
        ids = [uuid.uuid7() for _ in range(50)]
        for object_id in ids:
            for _ in range(2):  # an older message, then a newer one
                queue_security_email(
                    address="x@example.org",
                    target=Invitation(pk=object_id),
                    rule=ACCOUNT_ACTIVE,
                    subject="s",
                    body="b",
                )
            newest = EmailOutbox.objects.filter(eligibility_object_id=object_id).latest(
                "created_at"
            )
            EmailOutbox.objects.filter(pk=newest.pk).update(status=Status.SENT)
        assert ctx.has(Capability.ORG_MANAGE)  # loads the grants: the list itself is one query
        with CaptureQueriesContext(connection) as queries:
            found = selectors.delivery_status(ctx, selectors.INVITATION, ids)
        assert len(queries) == 1
        assert {status.state for status in found.values()} == {Status.SENT}
        assert set(found) == set(ids)

    def test_the_list_costs_the_same_for_one_invitation_or_many(
        self, org: Org, people: People, admin_client: Any
    ) -> None:
        invite(admin_client, org, people, "one@example.org")
        with CaptureQueriesContext(connection) as few:
            assert admin_client.get(reverse("org:invitations")).status_code == 200
        for index in range(12):
            invite(admin_client, org, people, f"more{index}@example.org")
        with CaptureQueriesContext(connection) as many:
            assert admin_client.get(reverse("org:invitations")).status_code == 200
        assert len(many) == len(few)


class TestAccess:
    def test_only_group_administrators_and_only_known_objects(
        self, org: Org, people: People, admin_client: Any
    ) -> None:
        invitation = invite(admin_client, org, people)
        change = TestEmailChange().change(people, admin_client)
        urls = [
            reverse("org:invitation_delivery", args=[invitation.pk]),
            reverse("org:email_change_delivery", args=[change.pk]),
        ]
        for name in ("mm_mad", "engineer", "wo_mad", "tech_mad"):
            client = client_for(getattr(people, name))
            assert [client.get(url).status_code for url in urls] == [403, 403], name
        unknown = uuid.uuid7()
        assert (
            admin_client.get(reverse("org:invitation_delivery", args=[unknown])).status_code == 404
        )
        assert (
            admin_client.get(reverse("org:email_change_delivery", args=[unknown])).status_code
            == 404
        )
        assert admin_client.post(urls[0]).status_code == 405
        shown = admin_client.get(urls[0])
        assert "no-store" in shown["Cache-Control"]

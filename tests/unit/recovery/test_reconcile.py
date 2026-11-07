"""``post_restore_reconcile``: a restored database resurrects nothing (plan §9, F24, F44, F52)."""

from collections.abc import Callable
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
from allauth.account.models import EmailAddress
from allauth.mfa.models import Authenticator
from allauth.mfa.recovery_codes.internal.auth import RecoveryCodes
from django.contrib.sessions.models import Session
from django.core import mail
from django.core.exceptions import ValidationError
from django.test import Client
from django.urls import reverse
from django.utils import timezone

from apps.access.exceptions import AccessDenied
from apps.access.principals import SessionPrincipal
from apps.access.roles import Role
from apps.accounts.models import EmailChangeRequest, Invitation, User
from apps.audit.models import AuditEvent
from apps.core.ids import new_id
from apps.notifications.models import EmailAttempt, EmailOutbox, Kind, Outcome, Status
from apps.notifications.outbox import queue_notification, queue_security_email
from apps.organizations.models import Branch, Company, Membership
from apps.projects.models import Project, ProjectAssignment
from apps.recovery import services
from apps.recovery.replay import (
    CONFIRMED_ACTION as CONFIRMED,
)
from apps.recovery.replay import (
    EVENT_REPLAYED as REPLAYED,
)
from apps.recovery.replay import (
    GAP_CREDENTIALS,
    GAP_OFFBOARDED,
)
from apps.recovery.replay import (
    SUSPENDED_ACTION as SUSPENDED,
)
from apps.recovery.services import Gap, post_restore_reconcile
from tests.org import Org, People, build_sentinel_order, client_for
from tests.outbox import make_dispatch
from tests.support import PASSWORD, error_codes, password_login, principal_for
from tests.unit.recovery.conftest import audit_event, state_of

pytestmark = pytest.mark.django_db

ROOT = Path(__file__).resolve().parents[3]


def reconcile(principal: SessionPrincipal, *events: dict[str, Any], **kwargs: Any) -> Any:
    outbox = kwargs.pop("outbox", ())
    return post_restore_reconcile(principal, failed_state=state_of(*events, outbox=outbox))


def invitation(email: str = "new@example.org", **fields: Any) -> Invitation:
    return Invitation.objects.create(
        email=email,
        kind=Invitation.Kind.NEW_ACCOUNT,
        token_hash=str(new_id()),
        expires_at=timezone.now() + timedelta(days=3),
        bootstrap=True,
        **fields,
    )


def moved(user: User, target: str | None) -> dict[str, Any]:
    """The audit row of an email change the failed database says was applied."""
    return audit_event(
        "email_change.applied",
        new_id(),
        {"user_id": str(user.pk), "from": user.email, "to": target},
        object_type="accounts.emailchangerequest",
    )


def email_change(user: User, requested_by: User) -> EmailChangeRequest:
    return EmailChangeRequest.objects.create(
        user=user,
        new_email="moved@example.org",
        requested_by=requested_by,
        token_hash=str(new_id()),
        cancel_token_hash=str(new_id()),
        expires_at=timezone.now() + timedelta(days=1),
    )


def security_mail(status: Status = Status.QUEUED, **fields: Any) -> EmailOutbox:
    target = User.objects.order_by("pk").first()
    assert target is not None
    message = queue_security_email(
        address="someone@example.org",
        target=target,
        rule="account.active",
        subject="Reset",
        body="https://example.org/accounts/password/reset/key/1-TOKEN/",
    )
    EmailOutbox.objects.filter(pk=message.pk).update(status=status, **fields)
    return message


class TestAuthority:
    def test_a_platform_operator_reconciles_and_the_audit_keeps_the_counts(
        self, principal: SessionPrincipal
    ) -> None:
        report = post_restore_reconcile(principal, failed_state=state_of())
        event = AuditEvent.objects.get(action="recovery.reconciled")
        assert event.data["failed_state_used"] is True
        assert report.failed_state_used is True

    def test_a_group_administrator_cannot_while_an_operator_exists(self, people: People) -> None:
        with pytest.raises(AccessDenied):
            post_restore_reconcile(principal_for(people.group_admin), failed_state=state_of())
        assert not AuditEvent.objects.filter(action="recovery.reconciled").exists()

    def test_a_group_administrator_can_when_the_installation_has_no_operator(
        self, people: People
    ) -> None:
        User.objects.filter(is_superuser=True).update(is_superuser=False)
        people.group_admin.refresh_from_db()
        report = post_restore_reconcile(principal_for(people.group_admin), failed_state=state_of())
        assert report.failed_state_used

    @pytest.mark.parametrize("who", ["tech_mad", "operator", "deactivated"])
    def test_nobody_else(self, people: People, who: str) -> None:
        with pytest.raises(AccessDenied):
            post_restore_reconcile(principal_for(getattr(people, who)), failed_state=state_of())

    def test_a_gap_is_refused_next_to_a_failed_state(self, principal: SessionPrincipal) -> None:
        with pytest.raises(ValidationError) as caught:
            post_restore_reconcile(principal, failed_state=state_of(), gap=Gap())
        assert error_codes(caught.value) == {"gap_with_state"}


class TestSessionsAndTokens:
    def test_every_session_ends(self, principal: SessionPrincipal, people: People) -> None:
        client = client_for(people.tech_mad)
        assert client.get(reverse("core:home")).status_code == 200
        epoch = User.objects.get(pk=people.tech_mad.pk).session_epoch
        report = reconcile(principal)
        assert report.sessions_deleted >= 1
        assert not Session.objects.exists()
        assert report.session_epochs_bumped == User.objects.count()
        assert User.objects.get(pk=people.tech_mad.pk).session_epoch == epoch + 1
        assert client.get(reverse("core:home")).status_code == 302

    def test_every_pending_invitation_and_email_change_is_revoked(
        self, principal: SessionPrincipal, people: People, operator: User
    ) -> None:
        pending = invitation()
        done = invitation("done@example.org", accepted_at=timezone.now())
        open_change = email_change(people.tech_mad, operator)
        applied = email_change(people.mm_mad, operator)
        EmailChangeRequest.objects.filter(pk=applied.pk).update(
            confirmed_at=timezone.now(), apply_after=timezone.now(), applied_at=timezone.now()
        )
        report = reconcile(principal)
        assert (report.invitations_revoked, report.email_changes_revoked) == (1, 1)
        pending.refresh_from_db()
        done.refresh_from_db()
        open_change.refresh_from_db()
        applied.refresh_from_db()
        assert pending.revoked_at is not None and pending.revoke_reason == services.REASON_RESTORE
        assert done.revoked_at is None
        assert open_change.revoked_at is not None
        assert applied.revoked_at is None

    @pytest.mark.parametrize(
        "status",
        [Status.QUEUED, Status.SENDING, Status.FAILED, Status.AMBIGUOUS, Status.QUARANTINED],
    )
    def test_every_unsent_security_email_is_wiped_and_expired(
        self, principal: SessionPrincipal, status: Status
    ) -> None:
        attempt = None
        message = security_mail()
        if status == Status.SENDING:
            attempt = EmailAttempt.objects.create(message=message, number=1)
            EmailOutbox.objects.filter(pk=message.pk).update(current_attempt=attempt)
        EmailOutbox.objects.filter(pk=message.pk).update(status=status)
        report = reconcile(principal)
        message.refresh_from_db()
        assert report.security_emails_expired == 1
        assert (message.status, message.body, message.html_body) == (Status.EXPIRED, "", "")
        assert message.wiped_at is not None
        assert report.outbox_quarantined == {}

    def test_a_security_email_already_wiped_only_expires(self, principal: SessionPrincipal) -> None:
        wiped_at = timezone.now() - timedelta(hours=1)
        message = security_mail()
        EmailOutbox.objects.filter(pk=message.pk).update(
            status=Status.FAILED, body="", html_body="", wiped_at=wiped_at
        )
        reconcile(principal)
        message.refresh_from_db()
        assert (message.status, message.wiped_at) == (Status.EXPIRED, wiped_at)

    def test_a_sent_security_email_stays_sent(self, principal: SessionPrincipal) -> None:
        message = security_mail()
        EmailOutbox.objects.filter(pk=message.pk).update(
            status=Status.SENT, body="", html_body="", wiped_at=timezone.now()
        )
        report = reconcile(principal)
        message.refresh_from_db()
        assert message.status == Status.SENT
        assert report.security_emails_expired == 0


class TestRecoveryCodes:
    def test_every_set_is_invalidated_and_every_owner_owes_a_new_one(
        self, principal: SessionPrincipal, people: People
    ) -> None:
        rc = RecoveryCodes.activate(people.tech_mad)
        used_before_the_restore = rc.generate_codes()[0]
        report = reconcile(principal)
        assert report.recovery_code_sets_invalidated == 1
        assert not Authenticator.objects.filter(type=Authenticator.Type.RECOVERY_CODES).exists()
        assert User.objects.get(pk=people.tech_mad.pk).recovery_codes_owed
        # The authenticator behind the code is gone: nothing can accept it.
        assert not Authenticator.objects.filter(
            user=people.tech_mad, type=Authenticator.Type.RECOVERY_CODES
        ).exists()
        assert used_before_the_restore

    def test_an_account_without_a_factor_owes_nothing(
        self, principal: SessionPrincipal, make_user: Callable[..., User]
    ) -> None:
        user = make_user("no-factor@example.org")
        reconcile(principal)
        user.refresh_from_db()
        assert not user.recovery_codes_owed


class TestOutbox:
    @pytest.mark.parametrize(
        "status", [Status.QUEUED, Status.SENDING, Status.FAILED, Status.AMBIGUOUS]
    )
    def test_a_restored_dispatch_comes_up_quarantined_and_nothing_is_sent(
        self, principal: SessionPrincipal, org: Org, people: People, status: Status
    ) -> None:
        order = build_sentinel_order(org, people).order
        message = make_dispatch(order, Status.QUEUED)
        if status == Status.SENDING:
            attempt = EmailAttempt.objects.create(message=message, number=1)
            EmailOutbox.objects.filter(pk=message.pk).update(current_attempt=attempt)
        EmailOutbox.objects.filter(pk=message.pk).update(status=status)
        report = reconcile(principal)
        message.refresh_from_db()
        assert message.status == Status.QUARANTINED
        assert report.outbox_quarantined == {Kind.PO_ORDER: 1}
        assert mail.outbox == []

    def test_a_restored_notification_comes_up_quarantined(
        self, principal: SessionPrincipal, people: People
    ) -> None:
        [message] = queue_notification(
            recipients=[people.tech_mad.pk],
            capability="catalog.view",
            obj=None,
            subject="Hello",
            body="Text",
        )
        report = reconcile(principal)
        message.refresh_from_db()
        assert message.status == Status.QUARANTINED
        assert report.outbox_quarantined == {Kind.NOTIFICATION: 1}

    @pytest.mark.parametrize(
        "terminal",
        [
            Status.SENT,
            Status.CANCELLED,
            Status.SUPERSEDED,
            Status.SUPPRESSED,
            Status.EXPIRED,
        ],
    )
    def test_a_terminal_outcome_in_the_failed_database_wins(
        self, principal: SessionPrincipal, org: Org, people: People, terminal: Status
    ) -> None:
        order = build_sentinel_order(org, people).order
        message = make_dispatch(order, Status.QUEUED)
        sent_at = timezone.now() if terminal == Status.SENT else None
        report = reconcile(
            principal,
            outbox=(
                {
                    "id": str(message.pk),
                    "status": terminal,
                    "sent_at": sent_at.isoformat() if sent_at else None,
                },
            ),
        )
        message.refresh_from_db()
        assert message.status == terminal
        assert message.sent_at == sent_at
        assert report.outbox_outcome_taken == {terminal: 1}
        assert report.outbox_quarantined == {}

    def test_a_row_still_unsettled_in_the_failed_database_is_quarantined(
        self, principal: SessionPrincipal, org: Org, people: People
    ) -> None:
        order = build_sentinel_order(org, people).order
        message = make_dispatch(order, Status.QUEUED)
        reconcile(
            principal, outbox=({"id": str(message.pk), "status": "AMBIGUOUS", "sent_at": None},)
        )
        message.refresh_from_db()
        assert message.status == Status.QUARANTINED

    def test_an_attempt_left_open_is_closed_as_ambiguous(
        self, principal: SessionPrincipal, org: Org, people: People
    ) -> None:
        order = build_sentinel_order(org, people).order
        message = make_dispatch(order, Status.QUEUED)
        attempt = EmailAttempt.objects.create(message=message, number=1)
        EmailOutbox.objects.filter(pk=message.pk).update(
            status=Status.SENDING, current_attempt=attempt
        )
        report = reconcile(principal)
        attempt.refresh_from_db()
        assert report.open_attempts_closed == 1
        assert (attempt.outcome, attempt.abandoned) == (Outcome.AMBIGUOUS, True)
        assert attempt.finished_at is not None


class TestReplay:
    def test_a_revocation_after_the_snapshot_is_applied_again(
        self, principal: SessionPrincipal, people: People
    ) -> None:
        membership = Membership.objects.get(user=people.tech_mad)
        when = timezone.now() - timedelta(hours=1)
        report = reconcile(
            principal,
            audit_event("membership.revoked", membership.pk, occurred_at=when),
        )
        membership.refresh_from_db()
        assert membership.revoked_at == when
        assert report.replayed == {"membership.revoked": 1}
        event = AuditEvent.objects.get(action="recovery.event_replayed")
        assert event.data["source_action"] == "membership.revoked"
        assert event.data["source_actor"] == "admin@example.org"

    def test_an_assignment_revocation_too(
        self, principal: SessionPrincipal, people: People
    ) -> None:
        assignment = ProjectAssignment.objects.filter(
            user=people.site_manager, revoked_at__isnull=True
        ).first()
        assert assignment is not None
        reconcile(principal, audit_event("project_assignment.revoked", assignment.pk))
        assignment.refresh_from_db()
        assert assignment.revoked_at is not None

    def test_an_event_the_restored_log_has_is_older_than_the_snapshot(
        self, principal: SessionPrincipal, people: People, operator: User
    ) -> None:
        membership = Membership.objects.get(user=people.tech_mad)
        restored = AuditEvent.objects.create(
            actor_label=operator.email, action="membership.revoked", data={}
        )
        report = reconcile(
            principal,
            audit_event("membership.revoked", membership.pk, id=restored.pk),
        )
        membership.refresh_from_db()
        assert membership.revoked_at is None
        assert report.replayed == {}

    def test_a_row_that_is_already_so_or_gone_counts_as_nothing_to_do(
        self, principal: SessionPrincipal, people: People
    ) -> None:
        revoked = Membership.objects.get(user=people.revoked)
        report = reconcile(
            principal,
            audit_event("membership.revoked", revoked.pk),
            audit_event("membership.revoked", new_id()),
            audit_event("user.offboarded", new_id()),
            audit_event("user.password_changed", new_id()),
            audit_event("user.mfa_reset", new_id()),
            audit_event("email_change.applied", new_id(), {"user_id": str(new_id())}),
        )
        assert report.replayed == {}
        assert report.already_so == {
            "membership.revoked": 2,
            "user.offboarded": 1,
            "user.password_changed": 1,
            "user.mfa_reset": 1,
            "email_change.applied": 1,
        }

    def test_a_shorter_validity_applies_and_a_longer_one_does_not(
        self, principal: SessionPrincipal, people: People
    ) -> None:
        shortened = Membership.objects.get(user=people.tech_mad)
        lengthened = Membership.objects.get(user=people.mm_mad)
        until = timezone.now() + timedelta(days=2)
        Membership.objects.filter(pk=lengthened.pk).update(valid_until=until)
        report = reconcile(
            principal,
            audit_event(
                "membership.validity_changed",
                shortened.pk,
                {"valid_until": {"from": None, "to": until.isoformat()}},
            ),
            audit_event(
                "membership.validity_changed",
                lengthened.pk,
                {"valid_until": {"from": until.isoformat(), "to": None}},
            ),
        )
        shortened.refresh_from_db()
        lengthened.refresh_from_db()
        assert shortened.valid_until == until
        assert lengthened.valid_until == until
        assert report.replayed == {"membership.validity_changed": 1}
        assert report.already_so == {"membership.validity_changed": 1}

    def test_a_later_start_applies_and_a_window_that_closes_revokes(
        self, principal: SessionPrincipal, people: People
    ) -> None:
        later = ProjectAssignment.objects.filter(
            user=people.site_manager, revoked_at__isnull=True
        ).first()
        closed = Membership.objects.get(user=people.tech_mad)
        start = timezone.now() + timedelta(days=5)
        assert later is not None
        reconcile(
            principal,
            audit_event(
                "project_assignment.validity_changed",
                later.pk,
                {"valid_from": {"from": None, "to": start.isoformat()}},
            ),
            audit_event(
                "membership.validity_changed",
                closed.pk,
                {"valid_until": {"from": None, "to": (closed.valid_from).isoformat()}},
            ),
        )
        later.refresh_from_db()
        closed.refresh_from_db()
        assert later.valid_from == start
        assert closed.revoked_at is not None

    def test_a_validity_change_of_a_grant_since_revoked_is_nothing_to_do(
        self, principal: SessionPrincipal, people: People
    ) -> None:
        revoked = Membership.objects.get(user=people.revoked)
        report = reconcile(principal, audit_event("membership.validity_changed", revoked.pk))
        assert report.already_so == {"membership.validity_changed": 1}

    def test_an_offboarding_applies_again_with_its_grants(
        self, principal: SessionPrincipal, people: People
    ) -> None:
        when = timezone.now() - timedelta(hours=2)
        report = reconcile(
            principal, audit_event("user.offboarded", people.site_manager.pk, occurred_at=when)
        )
        people.site_manager.refresh_from_db()
        assert not people.site_manager.is_active
        assert people.site_manager.offboarded_at == when
        assert not Membership.objects.filter(
            user=people.site_manager, revoked_at__isnull=True
        ).exists()
        assert not ProjectAssignment.objects.filter(
            user=people.site_manager, revoked_at__isnull=True
        ).exists()
        assert report.replayed == {"user.offboarded": 1}

    def test_a_person_offboarded_before_the_snapshot_stays_as_they_were(
        self, principal: SessionPrincipal, people: People
    ) -> None:
        when = timezone.now() - timedelta(days=9)
        User.objects.filter(pk=people.tech_mad.pk).update(is_active=False, offboarded_at=when)
        report = reconcile(principal, audit_event("user.offboarded", people.tech_mad.pk))
        people.tech_mad.refresh_from_db()
        assert people.tech_mad.offboarded_at == when
        assert report.already_so == {"user.offboarded": 1}

    def test_an_mfa_reset_removes_nothing_and_waits_for_a_human(
        self, principal: SessionPrincipal, people: People
    ) -> None:
        """A planted row must not leave only the password between the account and an attacker."""
        user = people.tech_mad
        assert Authenticator.objects.filter(user=user).exists()
        report = reconcile(principal, audit_event("user.mfa_reset", user.pk))
        assert Authenticator.objects.filter(user=user).exists()
        user.refresh_from_db()
        assert not user.is_active
        assert user.offboarded_at is None
        assert report.replayed == {"user.mfa_reset": 1}
        assert report.suspended == [user.email]
        assert report.factor_removals_held == {user.email: ["all"]}
        assert services.held_factor_removal(user) == [Authenticator.Type.TOTP]

    def test_a_removed_factor_holds_only_that_kind(
        self, principal: SessionPrincipal, people: People
    ) -> None:
        user = people.tech_mad
        Authenticator.objects.create(user=user, type=Authenticator.Type.WEBAUTHN, data={})
        report = reconcile(
            principal, audit_event("user.authenticator_removed", user.pk, {"type": "totp"})
        )
        assert Authenticator.objects.filter(user=user).count() == 2
        assert report.factor_removals_held == {user.email: ["totp"]}
        assert services.held_factor_removal(user) == [Authenticator.Type.TOTP]

    def test_several_removals_make_one_report_line_and_one_question(
        self, principal: SessionPrincipal, people: People
    ) -> None:
        user = people.tech_mad
        Authenticator.objects.create(user=user, type=Authenticator.Type.WEBAUTHN, data={})
        report = reconcile(
            principal,
            audit_event("user.authenticator_removed", user.pk, {"type": "totp"}),
            audit_event("user.authenticator_removed", user.pk, {"type": "totp"}),
            audit_event("user.authenticator_removed", user.pk, {"type": "webauthn"}),
        )
        assert report.suspended == [user.email]
        assert report.factor_removals_held == {user.email: ["totp", "webauthn"]}
        assert services.held_factor_removal(user) == ["totp", "webauthn"]

    def test_a_factor_the_account_no_longer_has_is_nothing_to_do(
        self, principal: SessionPrincipal, people: People
    ) -> None:
        user = people.tech_mad
        report = reconcile(
            principal, audit_event("user.authenticator_removed", user.pk, {"type": "webauthn"})
        )
        assert report.already_so == {"user.authenticator_removed": 1}
        user.refresh_from_db()
        assert user.is_active

    def test_recovery_codes_are_no_question(
        self, principal: SessionPrincipal, people: People
    ) -> None:
        """Every restored set goes anyway; nobody is suspended over one."""
        user = people.tech_mad
        report = reconcile(
            principal,
            audit_event("user.authenticator_removed", user.pk, {"type": "recovery_codes"}),
        )
        assert report.already_so == {"user.authenticator_removed": 1}
        user.refresh_from_db()
        assert user.is_active

    @pytest.mark.parametrize("kind", ["", "evil\x1b[31m", None])
    def test_a_kind_that_is_not_one_is_reported_and_touches_nothing(
        self, principal: SessionPrincipal, people: People, kind: str | None
    ) -> None:
        user = people.tech_mad
        report = reconcile(
            principal, audit_event("user.authenticator_removed", user.pk, {"type": kind})
        )
        assert report.not_replayed == {"user.authenticator_removed": 1}
        user.refresh_from_db()
        assert user.is_active

    def test_an_offboarded_account_has_no_factor_to_hold(
        self, principal: SessionPrincipal, people: People
    ) -> None:
        user = people.tech_mad
        User.objects.filter(pk=user.pk).update(is_active=False, offboarded_at=timezone.now())
        report = reconcile(principal, audit_event("user.mfa_reset", user.pk))
        assert report.already_so == {"user.mfa_reset": 1}
        assert report.factor_removals_held == {}

    def test_the_operator_keeps_their_factors_and_stays_in(
        self, principal: SessionPrincipal, operator: User
    ) -> None:
        """Suspending them could leave nobody to confirm anyone; the report says what is held."""
        Authenticator.objects.get_or_create(
            user=operator, type=Authenticator.Type.TOTP, defaults={"data": {}}
        )
        report = reconcile(principal, audit_event("user.mfa_reset", operator.pk))
        operator.refresh_from_db()
        assert operator.is_active
        assert Authenticator.objects.filter(user=operator).exists()
        assert report.own_factor_removal == ["all"]
        assert report.factor_removals_held == {operator.email: ["all"]}
        assert report.suspended == []

    def test_a_changed_password_cannot_be_used_any_more(
        self, principal: SessionPrincipal, people: People, client: Client
    ) -> None:
        reconcile(
            principal,
            audit_event("user.password_changed", people.tech_mad.pk, {"how": "changed"}),
        )
        people.tech_mad.refresh_from_db()
        assert not people.tech_mad.has_usable_password()

    def test_an_email_change_moves_nothing_and_waits_for_a_human(
        self, principal: SessionPrincipal, people: People
    ) -> None:
        """A row planted with the runtime credentials must not hand the account to its address,
        and the restored address must not get the account back either."""
        user = people.tech_mad
        old = user.email
        EmailAddress.objects.create(user=user, email=old, primary=True, verified=True)
        report = reconcile(principal, moved(user, "moved@example.org"))
        user.refresh_from_db()
        assert user.email == old
        assert not user.is_active
        assert user.offboarded_at is None
        assert list(EmailAddress.objects.filter(user=user).values_list("email", flat=True)) == [old]
        assert report.replayed == {"email_change.applied": 1}
        assert report.suspended == [old]
        assert report.email_changes_held == {old: "moved@example.org"}
        assert services.held_email_change(user) == "moved@example.org"

    def test_while_it_waits_neither_address_signs_in_or_gets_a_reset(
        self, principal: SessionPrincipal, people: People, client: Client
    ) -> None:
        user = people.tech_mad
        old = user.email
        EmailAddress.objects.create(user=user, email=old, primary=True, verified=True)
        reconcile(principal, moved(user, "moved@example.org"))
        queued = EmailOutbox.objects.count()
        for address in (old, "moved@example.org"):
            password_login(client, address, PASSWORD)
            assert "_auth_user_id" not in client.session
            client.post(reverse("account_reset_password"), {"email": address})
        assert EmailOutbox.objects.count() == queued

    def test_the_latest_change_is_the_one_to_decide_and_one_report_line_is_enough(
        self, principal: SessionPrincipal, people: People
    ) -> None:
        user = people.tech_mad
        report = reconcile(
            principal,
            moved(user, "first@example.org"),
            moved(user, "second@example.org"),
        )
        assert report.suspended == [user.email]
        assert report.email_changes_held == {user.email: "second@example.org"}
        assert services.held_email_change(user) == "second@example.org"

    def test_a_change_there_and_back_still_waits_but_leaves_nothing_to_decide(
        self, principal: SessionPrincipal, people: People
    ) -> None:
        user = people.tech_mad
        reconcile(principal, moved(user, "away@example.org"), moved(user, user.email))
        user.refresh_from_db()
        assert not user.is_active
        assert services.held_email_change(user) == ""

    def test_an_email_change_already_so_is_nothing_to_do(
        self, principal: SessionPrincipal, people: People
    ) -> None:
        user = people.tech_mad
        report = reconcile(principal, moved(user, user.email))
        assert report.already_so == {"email_change.applied": 1}
        user.refresh_from_db()
        assert user.is_active

    def test_the_operator_stays_in_and_the_report_says_what_is_held(
        self, principal: SessionPrincipal, operator: User
    ) -> None:
        """Suspending them could leave nobody to confirm anyone, and applying it would hand the
        account most worth taking to whoever planted the row."""
        old = operator.email
        report = reconcile(principal, moved(operator, "operator-moved@example.org"))
        operator.refresh_from_db()
        assert operator.is_active
        assert operator.email == old
        assert report.own_email_change == "operator-moved@example.org"
        assert report.email_changes_held == {old: "operator-moved@example.org"}
        assert report.suspended == []

    def test_an_offboarded_account_has_nothing_to_hold(
        self, principal: SessionPrincipal, people: People
    ) -> None:
        user = people.tech_mad
        User.objects.filter(pk=user.pk).update(is_active=False, offboarded_at=timezone.now())
        report = reconcile(principal, moved(user, "gone@example.org"))
        assert report.already_so == {"email_change.applied": 1}
        assert report.email_changes_held == {}

    @pytest.mark.parametrize("target", ["", "not an address", "a@b\x1b[31m.example", None])
    def test_an_address_that_is_not_one_is_reported_and_touches_nothing(
        self, principal: SessionPrincipal, people: People, target: str | None
    ) -> None:
        user = people.tech_mad
        report = reconcile(principal, moved(user, target))
        assert report.not_replayed == {"email_change.applied": 1}
        user.refresh_from_db()
        assert user.is_active

    def test_an_account_id_that_is_not_one_touches_nothing(
        self, principal: SessionPrincipal
    ) -> None:
        report = reconcile(
            principal,
            audit_event("email_change.applied", new_id(), {"user_id": "x", "to": "a@example.org"}),
            audit_event("email_change.applied", new_id(), {"to": "a@example.org"}),
        )
        assert report.already_so == {"email_change.applied": 2}

    @pytest.mark.parametrize(
        ("action", "model", "attribute"),
        [
            ("company.updated", Company, "c"),
            ("branch.updated", Branch, "a_sev"),
        ],
    )
    def test_a_deactivation_applies_and_other_changes_do_not(
        self,
        principal: SessionPrincipal,
        org: Org,
        action: str,
        model: Any,
        attribute: str,
    ) -> None:
        row = getattr(org, attribute)
        report = reconcile(
            principal,
            audit_event(action, row.pk, {"legal_name": {"from": "A", "to": "B"}}),
            audit_event(action, row.pk, {"is_active": {"from": False, "to": True}}),
            audit_event(action, row.pk, {"is_active": {"from": True, "to": False}}),
            audit_event(action, row.pk, {"is_active": {"from": True, "to": False}}),
        )
        row.refresh_from_db()
        assert row.is_active is False
        assert report.replayed == {action: 1}
        assert report.already_so == {action: 1}
        assert report.not_replayed == {action: 2}

    def test_a_project_put_on_hold_stays_so(self, principal: SessionPrincipal, org: Org) -> None:
        project: Project = org.hospital
        report = reconcile(
            principal,
            audit_event("project.updated", project.pk, {"name": {"from": "A", "to": "B"}}),
            audit_event("project.updated", project.pk, {"status": {"from": "X", "to": "ACTIVE"}}),
            audit_event(
                "project.updated", project.pk, {"status": {"from": "ACTIVE", "to": "ON_HOLD"}}
            ),
        )
        project.refresh_from_db()
        assert project.status == Project.Status.ON_HOLD
        assert report.not_replayed == {"project.updated": 2}

    def test_what_gives_access_is_never_replayed_but_is_reported(
        self, principal: SessionPrincipal, people: People
    ) -> None:
        report = reconcile(
            principal,
            audit_event("membership.granted", new_id()),
            audit_event("invitation.accepted", new_id()),
        )
        assert report.not_replayed == {"membership.granted": 1, "invitation.accepted": 1}


class TestASecondRestore:
    """What the first restore did lives only in its own events, once the original rows did not
    come back; restoring the same or an older dump must not give any of it back."""

    def _replayed(self, source_action: str) -> dict[str, Any]:
        return dict(AuditEvent.objects.get(action=REPLAYED, data__source_action=source_action).data)

    def _again(self, operator: User) -> SessionPrincipal:
        """The first restore ended every session, the operator's too."""
        operator.refresh_from_db()
        return principal_for(operator)

    def test_a_revocation_replayed_once_is_replayed_again(
        self, principal: SessionPrincipal, operator: User, people: People
    ) -> None:
        membership = Membership.objects.get(user=people.mm_mad)
        reconcile(
            principal,
            audit_event(
                "membership.revoked", membership.pk, object_type="organizations.membership"
            ),
        )
        data = self._replayed("membership.revoked")
        Membership.objects.filter(pk=membership.pk).update(revoked_at=None, revoked_by=None)
        report = reconcile(self._again(operator), audit_event(REPLAYED, None, data, object_type=""))
        assert report.replayed == {"membership.revoked": 1}
        membership.refresh_from_db()
        assert membership.revoked_at is not None

    def test_what_the_event_said_comes_with_it(
        self, principal: SessionPrincipal, operator: User, people: People
    ) -> None:
        """A narrowed validity is nothing without the dates of the original row."""
        membership = Membership.objects.get(user=people.mm_mad)
        until = timezone.now() + timedelta(days=1)
        reconcile(
            principal,
            audit_event(
                "membership.validity_changed",
                membership.pk,
                {"valid_until": {"from": None, "to": until.isoformat()}},
                object_type="organizations.membership",
            ),
        )
        data = self._replayed("membership.validity_changed")
        assert data["source_data"]["valid_until"]["to"] == until.isoformat()
        Membership.objects.filter(pk=membership.pk).update(valid_until=None)
        report = reconcile(self._again(operator), audit_event(REPLAYED, None, data, object_type=""))
        assert report.replayed == {"membership.validity_changed": 1}
        membership.refresh_from_db()
        assert membership.valid_until == until

    def test_an_original_the_dump_has_is_older_than_the_snapshot(
        self, principal: SessionPrincipal, people: People
    ) -> None:
        """A newer dump holds the original row: nothing to replay from the record of it."""
        membership = Membership.objects.get(user=people.mm_mad)
        restored = AuditEvent.objects.create(
            actor_label="x",
            action="membership.revoked",
            object_type="organizations.membership",
            object_id=membership.pk,
        )
        data = {
            "source_event": str(restored.pk),
            "source_action": "membership.revoked",
            "source_actor": "x",
            "object_type": "organizations.membership",
            "object_id": str(membership.pk),
            "occurred_at": timezone.now().isoformat(),
            "source_data": {},
        }
        report = reconcile(principal, audit_event(REPLAYED, None, data, object_type=""))
        assert report.replayed == {}
        membership.refresh_from_db()
        assert membership.revoked_at is None

    def test_a_record_without_what_it_needs_is_reported_not_replayed(
        self, principal: SessionPrincipal
    ) -> None:
        report = reconcile(principal, audit_event(REPLAYED, None, {"source_action": "x"}))
        assert report.not_replayed == {REPLAYED: 1}

    def test_an_account_it_suspended_comes_up_suspended(
        self, principal: SessionPrincipal, people: People
    ) -> None:
        user = people.mm_mad
        report = reconcile(principal, audit_event(SUSPENDED, user.pk, {"reason": "no state"}))
        assert report.replayed == {SUSPENDED: 1}
        assert report.suspended == [user.email]
        user.refresh_from_db()
        assert user.is_active is False

    def test_an_account_already_suspended_or_gone_is_nothing_to_do(
        self, principal: SessionPrincipal, people: People
    ) -> None:
        User.objects.filter(pk=people.mm_mad.pk).update(is_active=False)
        report = reconcile(
            principal,
            audit_event(SUSPENDED, people.mm_mad.pk),
            audit_event(SUSPENDED, new_id()),
        )
        assert report.already_so == {SUSPENDED: 2}

    def test_a_confirmation_gives_nothing_back(
        self, principal: SessionPrincipal, people: People
    ) -> None:
        """An account suspended and confirmed before the failure waits to be confirmed again."""
        user = people.mm_mad
        report = reconcile(
            principal,
            audit_event(SUSPENDED, user.pk),
            audit_event(CONFIRMED, user.pk, {"credentials_changed": False}),
        )
        assert report.not_replayed == {CONFIRMED: 1}
        user.refresh_from_db()
        assert user.is_active is False

    @pytest.mark.parametrize("decision", [services.ACCEPT, services.REJECT])
    def test_a_decided_email_change_is_decided_again(
        self, principal: SessionPrincipal, operator: User, people: People, decision: str
    ) -> None:
        """A second restore goes back before the decision: only a human can make it again."""
        user = people.tech_mad
        old = user.email
        change = moved(user, "moved@example.org")
        reconcile(principal, change)
        services.confirm_suspended_account(self._again(operator), user.pk, email_change=decision)
        User.objects.filter(pk=user.pk).update(is_active=True, email=old)
        report = reconcile(
            self._again(operator),
            change,
            audit_event(
                CONFIRMED, user.pk, {"credentials_changed": False, "email_change": decision}
            ),
        )
        user.refresh_from_db()
        assert not user.is_active
        assert user.email == old
        assert report.email_changes_held == {old: "moved@example.org"}
        assert services.held_email_change(user) == "moved@example.org"

    def test_an_account_suspended_over_an_email_change_keeps_it_when_suspended_again(
        self, principal: SessionPrincipal, people: People
    ) -> None:
        user = people.tech_mad
        reconcile(
            principal,
            audit_event(SUSPENDED, user.pk, {"reason": "held", "email_to": "moved@example.org"}),
        )
        assert services.held_email_change(user) == "moved@example.org"

    def test_an_account_suspended_over_its_factors_keeps_them_held_when_suspended_again(
        self, principal: SessionPrincipal, people: People
    ) -> None:
        user = people.tech_mad
        reconcile(
            principal, audit_event(SUSPENDED, user.pk, {"reason": "held", "factors": ["totp"]})
        )
        assert services.held_factor_removal(user) == [Authenticator.Type.TOTP]

    @pytest.mark.parametrize("factors", [["evil"], "totp", 3, None])
    def test_a_suspension_row_holds_only_what_is_a_factor(
        self, principal: SessionPrincipal, people: People, factors: Any
    ) -> None:
        user = people.tech_mad
        reconcile(principal, audit_event(SUSPENDED, user.pk, {"reason": "x", "factors": factors}))
        assert services.held_factor_removal(user) == []
        assert not AuditEvent.objects.filter(
            action=SUSPENDED, object_id=user.pk, data__has_key="factors"
        ).exists()

    def test_a_row_saying_credentials_started_over_takes_the_password_and_holds_the_factors(
        self, principal: SessionPrincipal, people: People
    ) -> None:
        """Planted, it must not remove a second factor; and a password makes nobody an owner."""
        user = people.tech_mad
        assert Authenticator.objects.filter(user=user).exists()
        report = reconcile(principal, audit_event(GAP_CREDENTIALS, user.pk))
        user.refresh_from_db()
        assert not user.has_usable_password()
        assert not user.is_active
        assert Authenticator.objects.filter(user=user).exists()
        assert report.factor_removals_held == {user.email: ["all"]}

    @pytest.mark.parametrize("decision", [services.ACCEPT, services.REJECT])
    def test_a_decided_factor_removal_is_decided_again(
        self, principal: SessionPrincipal, operator: User, people: People, decision: str
    ) -> None:
        user = people.tech_mad
        change = audit_event("user.mfa_reset", user.pk)
        reconcile(principal, change)
        services.confirm_suspended_account(self._again(operator), user.pk, factor_removal=decision)
        # The second restore brings back the snapshot: active, with its factor.
        User.objects.filter(pk=user.pk).update(is_active=True)
        Authenticator.objects.get_or_create(
            user=user, type=Authenticator.Type.TOTP, defaults={"data": {}}
        )
        report = reconcile(self._again(operator), change)
        user.refresh_from_db()
        assert not user.is_active
        assert report.factor_removals_held == {user.email: ["all"]}
        assert services.held_factor_removal(user) == [Authenticator.Type.TOTP]

    def test_a_suspension_with_no_state_does_not_lose_a_held_email_change(
        self, principal: SessionPrincipal, operator: User, people: People
    ) -> None:
        """A later restore without a failed database suspends the account for another reason."""
        user = people.mm_mad
        reconcile(principal, moved(user, "moved@example.org"))
        post_restore_reconcile(self._again(operator), failed_state=None)
        assert services.held_email_change(user) == "moved@example.org"

    def test_credentials_that_started_over_stay_distrusted(
        self, principal: SessionPrincipal, people: People
    ) -> None:
        user = people.tech_mad
        assert user.has_usable_password()
        report = reconcile(
            principal,
            audit_event(CONFIRMED, user.pk, {"credentials_changed": True}),
            audit_event(CONFIRMED, new_id(), {"credentials_changed": True}),
        )
        assert report.replayed == {CONFIRMED: 1}
        assert report.already_so == {CONFIRMED: 1}
        user.refresh_from_db()
        assert not user.has_usable_password()

    def test_who_a_human_named_for_a_gap_is_audited_and_replayed(
        self, principal: SessionPrincipal, operator: User, people: People
    ) -> None:
        gone, changed = people.tech_mad, people.wo_mad
        post_restore_reconcile(
            principal,
            failed_state=None,
            gap=Gap(
                offboarded=frozenset({gone.email}), credentials_changed=frozenset({changed.email})
            ),
        )
        assert AuditEvent.objects.get(action=GAP_OFFBOARDED).object_id == gone.pk
        assert AuditEvent.objects.get(action=GAP_CREDENTIALS).object_id == changed.pk
        User.objects.filter(pk=gone.pk).update(is_active=True, offboarded_at=None)
        changed.set_password(PASSWORD)
        changed.save(update_fields=["password"])
        report = reconcile(
            self._again(operator),
            audit_event(GAP_OFFBOARDED, gone.pk),
            audit_event(GAP_CREDENTIALS, changed.pk),
        )
        assert report.replayed == {GAP_OFFBOARDED: 1, GAP_CREDENTIALS: 1}
        gone.refresh_from_db()
        changed.refresh_from_db()
        assert gone.offboarded_at is not None
        assert not changed.has_usable_password()

    def test_the_export_carries_the_family(self) -> None:
        sql = (ROOT / "docker_compose/gmat_m8/ops/failed-state.sql").read_text(encoding="utf-8")
        assert "|recovery)" in sql


class TestWithoutAFailedState:
    def test_every_privileged_account_but_the_operator_is_suspended(
        self, principal: SessionPrincipal, people: People, operator: User
    ) -> None:
        report = post_restore_reconcile(principal, failed_state=None)
        for who in (people.mm_mad, people.engineer, people.operator):
            who.refresh_from_db()
            assert not who.is_active, who.email
            assert who.offboarded_at is None
            assert who.email in report.suspended
        operator.refresh_from_db()
        people.tech_mad.refresh_from_db()
        assert operator.is_active
        assert people.tech_mad.is_active
        assert operator.email not in report.suspended
        assert people.tech_mad.email not in report.suspended
        assert AuditEvent.objects.filter(
            action="recovery.account_suspended", object_id=people.mm_mad.pk
        ).exists()

    def test_a_suspended_account_cannot_sign_in(
        self, principal: SessionPrincipal, people: People, client: Client
    ) -> None:
        post_restore_reconcile(principal, failed_state=None)
        response = password_login(client, people.mm_mad.email, PASSWORD)
        assert "_auth_user_id" not in client.session
        assert response.status_code in {200, 302}

    def test_accounts_the_maintainer_lists_are_offboarded_or_start_over(
        self, principal: SessionPrincipal, people: People
    ) -> None:
        report = post_restore_reconcile(
            principal,
            failed_state=None,
            gap=Gap(
                offboarded=frozenset({f" {people.tech_mad.email.upper()} "}),
                credentials_changed=frozenset({people.wo_mad.email}),
            ),
        )
        people.tech_mad.refresh_from_db()
        people.wo_mad.refresh_from_db()
        assert people.tech_mad.offboarded_at is not None
        assert not people.tech_mad.is_active
        assert not Membership.objects.filter(user=people.tech_mad, revoked_at=None).exists()
        assert not people.wo_mad.has_usable_password()
        assert not Authenticator.objects.filter(user=people.wo_mad).exists()
        assert people.wo_mad.is_active
        assert (report.gap_offboarded, report.gap_credentials_reset) == (1, 1)

    def test_an_address_nobody_has_stops_everything(self, principal: SessionPrincipal) -> None:
        with pytest.raises(ValidationError) as caught:
            post_restore_reconcile(
                principal, failed_state=None, gap=Gap(offboarded=frozenset({"typo@example.org"}))
            )
        assert error_codes(caught.value) == {"unknown_address"}
        assert not AuditEvent.objects.filter(action="recovery.reconciled").exists()

    def test_a_revoked_or_expired_privileged_role_is_not_privileged(
        self, principal: SessionPrincipal, people: People
    ) -> None:
        post_restore_reconcile(principal, failed_state=None)
        people.revoked.refresh_from_db()
        assert people.revoked.is_active


class TestConfirm:
    @pytest.fixture
    def suspended(self, principal: SessionPrincipal, people: People) -> User:
        post_restore_reconcile(principal, failed_state=None)
        people.mm_mad.refresh_from_db()
        return people.mm_mad

    def test_the_maintainer_vouches_and_the_account_signs_in(
        self, operator: User, suspended: User
    ) -> None:
        assert suspended in services.suspended_accounts()
        confirmed = services.confirm_suspended_account(principal_for(operator), suspended.pk)
        assert confirmed.is_active
        assert confirmed.has_usable_password()
        assert Authenticator.objects.filter(user=suspended).exists()
        assert suspended not in services.suspended_accounts()
        event = AuditEvent.objects.get(action="recovery.account_confirmed")
        assert event.data == {"credentials_changed": False}

    def test_credentials_that_changed_in_the_gap_start_over(
        self, operator: User, suspended: User
    ) -> None:
        confirmed = services.confirm_suspended_account(
            principal_for(operator), suspended.pk, credentials_changed=True
        )
        assert confirmed.is_active
        assert not confirmed.has_usable_password()
        assert not Authenticator.objects.filter(user=suspended).exists()

    def test_only_a_suspended_account_is_confirmed(
        self, principal: SessionPrincipal, people: People
    ) -> None:
        for who in (people.tech_mad, people.deactivated):
            with pytest.raises(ValidationError) as caught:
                services.confirm_suspended_account(principal, who.pk)
            assert error_codes(caught.value) == {"not_suspended"}

    def test_an_offboarded_account_stays_offboarded(self, operator: User, suspended: User) -> None:
        User.objects.filter(pk=suspended.pk).update(offboarded_at=timezone.now())
        with pytest.raises(ValidationError):
            services.confirm_suspended_account(principal_for(operator), suspended.pk)
        assert suspended not in services.suspended_accounts()

    def test_only_a_platform_operator_confirms_a_staff_account(
        self, principal: SessionPrincipal, people: People, member: Callable[..., User]
    ) -> None:
        post_restore_reconcile(principal, failed_state=None)
        other = member(Role.GROUP_ADMIN)
        with pytest.raises(AccessDenied):
            services.confirm_suspended_account(principal_for(other), people.operator.pk)

    def test_who_lacks_the_capability_cannot(self, suspended: User, people: People) -> None:
        with pytest.raises(AccessDenied):
            services.confirm_suspended_account(principal_for(people.tech_mad), suspended.pk)


class TestAHeldEmailChange:
    @pytest.fixture
    def held(self, principal: SessionPrincipal, people: People) -> User:
        user = people.tech_mad
        EmailAddress.objects.create(user=user, email=user.email, primary=True, verified=True)
        reconcile(principal, moved(user, "moved@example.org"))
        user.refresh_from_db()
        return user

    def _operator(self, operator: User) -> SessionPrincipal:
        operator.refresh_from_db()
        return principal_for(operator)

    def test_accepting_moves_the_account_and_its_address(self, operator: User, held: User) -> None:
        old = held.email
        confirmed = services.confirm_suspended_account(
            self._operator(operator), held.pk, email_change=services.ACCEPT
        )
        assert confirmed.is_active
        assert confirmed.email == "moved@example.org"
        assert list(EmailAddress.objects.filter(user=held).values_list("email", "verified")) == [
            ("moved@example.org", True)
        ]
        assert confirmed not in services.suspended_accounts()
        assert AuditEvent.objects.get(action=CONFIRMED).data == {
            "credentials_changed": False,
            "email_change": services.ACCEPT,
            "email_from": old,
            "email_to": "moved@example.org",
        }

    def test_rejecting_keeps_the_restored_address(self, operator: User, held: User) -> None:
        old = held.email
        confirmed = services.confirm_suspended_account(
            self._operator(operator), held.pk, email_change=services.REJECT
        )
        assert confirmed.is_active
        assert confirmed.email == old
        assert list(EmailAddress.objects.filter(user=held).values_list("email", flat=True)) == [old]
        assert AuditEvent.objects.get(action=CONFIRMED).data["email_change"] == "reject"
        assert services.held_email_change(confirmed) == ""

    def test_it_cannot_be_confirmed_without_a_decision(self, operator: User, held: User) -> None:
        with pytest.raises(ValidationError) as caught:
            services.confirm_suspended_account(self._operator(operator), held.pk)
        assert error_codes(caught.value) == {"email_change_undecided"}
        held.refresh_from_db()
        assert not held.is_active

    def test_a_decision_must_be_one_of_the_two(self, operator: User, held: User) -> None:
        with pytest.raises(ValidationError) as caught:
            services.confirm_suspended_account(
                self._operator(operator), held.pk, email_change="maybe"
            )
        assert error_codes(caught.value) == {"email_change_undecided"}

    def test_an_address_another_account_has_is_refused_and_changes_nothing(
        self, principal: SessionPrincipal, operator: User, people: People
    ) -> None:
        user = people.tech_mad
        old = user.email
        reconcile(principal, moved(user, people.mm_mad.email))
        with pytest.raises(ValidationError) as caught:
            services.confirm_suspended_account(
                self._operator(operator), user.pk, email_change=services.ACCEPT
            )
        assert error_codes(caught.value) == {"email_taken"}
        user.refresh_from_db()
        assert (user.email, user.is_active) == (old, False)

    def test_nothing_held_nothing_to_decide(
        self, principal: SessionPrincipal, operator: User, people: People
    ) -> None:
        post_restore_reconcile(principal, failed_state=None)
        with pytest.raises(ValidationError) as caught:
            services.confirm_suspended_account(
                self._operator(operator), people.mm_mad.pk, email_change=services.ACCEPT
            )
        assert error_codes(caught.value) == {"no_email_change"}

    def test_a_new_change_after_a_decision_is_a_new_decision(
        self, operator: User, held: User
    ) -> None:
        services.confirm_suspended_account(
            self._operator(operator), held.pk, email_change=services.REJECT
        )
        reconcile(self._operator(operator), moved(held, "again@example.org"))
        held.refresh_from_db()
        assert not held.is_active
        assert services.held_email_change(held) == "again@example.org"

    def test_credentials_can_start_over_in_the_same_step(self, operator: User, held: User) -> None:
        confirmed = services.confirm_suspended_account(
            self._operator(operator),
            held.pk,
            credentials_changed=True,
            email_change=services.ACCEPT,
        )
        assert confirmed.email == "moved@example.org"
        assert not confirmed.has_usable_password()


class TestAHeldFactorRemoval:
    @pytest.fixture
    def held(self, principal: SessionPrincipal, people: People) -> User:
        user = people.tech_mad
        reconcile(principal, audit_event("user.mfa_reset", user.pk))
        user.refresh_from_db()
        return user

    def _operator(self, operator: User) -> SessionPrincipal:
        operator.refresh_from_db()
        return principal_for(operator)

    def test_while_it_waits_the_account_cannot_sign_in(
        self, people: People, client: Client, held: User
    ) -> None:
        password_login(client, held.email, PASSWORD)
        assert "_auth_user_id" not in client.session

    def test_accepting_removes_them_and_owes_no_codes(self, operator: User, held: User) -> None:
        assert held.recovery_codes_owed
        confirmed = services.confirm_suspended_account(
            self._operator(operator), held.pk, factor_removal=services.ACCEPT
        )
        assert confirmed.is_active
        assert confirmed.has_usable_password()
        assert not Authenticator.objects.filter(user=held).exists()
        assert not confirmed.recovery_codes_owed
        assert AuditEvent.objects.get(action=CONFIRMED).data == {
            "credentials_changed": False,
            "factor_removal": services.ACCEPT,
            "factors": [Authenticator.Type.TOTP],
        }
        assert services.held_factor_removal(confirmed) == []

    def test_rejecting_keeps_them(self, operator: User, held: User) -> None:
        confirmed = services.confirm_suspended_account(
            self._operator(operator), held.pk, factor_removal=services.REJECT
        )
        assert confirmed.is_active
        assert Authenticator.objects.filter(user=held, type=Authenticator.Type.TOTP).exists()
        assert confirmed.recovery_codes_owed
        assert AuditEvent.objects.get(action=CONFIRMED).data["factor_removal"] == services.REJECT
        assert services.held_factor_removal(confirmed) == []

    def test_accepting_one_kind_keeps_the_others_and_their_debt(
        self, principal: SessionPrincipal, operator: User, people: People
    ) -> None:
        user = people.tech_mad
        Authenticator.objects.create(user=user, type=Authenticator.Type.WEBAUTHN, data={})
        reconcile(principal, audit_event("user.authenticator_removed", user.pk, {"type": "totp"}))
        confirmed = services.confirm_suspended_account(
            self._operator(operator), user.pk, factor_removal=services.ACCEPT
        )
        left = Authenticator.objects.filter(user=user).values_list("type", flat=True)
        assert list(left) == [Authenticator.Type.WEBAUTHN]
        assert confirmed.recovery_codes_owed

    @pytest.mark.parametrize("decision", ["", "maybe"])
    def test_it_cannot_be_confirmed_without_a_decision(
        self, operator: User, held: User, decision: str
    ) -> None:
        with pytest.raises(ValidationError) as caught:
            services.confirm_suspended_account(
                self._operator(operator), held.pk, factor_removal=decision
            )
        assert error_codes(caught.value) == {"factor_removal_undecided"}
        held.refresh_from_db()
        assert not held.is_active

    def test_nothing_held_nothing_to_decide(
        self, principal: SessionPrincipal, operator: User, people: People
    ) -> None:
        post_restore_reconcile(principal, failed_state=None)
        with pytest.raises(ValidationError) as caught:
            services.confirm_suspended_account(
                self._operator(operator), people.mm_mad.pk, factor_removal=services.ACCEPT
            )
        assert error_codes(caught.value) == {"no_factor_removal"}

    def test_starting_over_answers_the_question(self, operator: User, held: User) -> None:
        confirmed = services.confirm_suspended_account(
            self._operator(operator), held.pk, credentials_changed=True
        )
        assert not confirmed.has_usable_password()
        assert not Authenticator.objects.filter(user=held).exists()
        assert not confirmed.recovery_codes_owed
        assert AuditEvent.objects.get(action=CONFIRMED).data["factor_removal"] == services.ACCEPT

    def test_starting_over_contradicts_a_rejection(self, operator: User, held: User) -> None:
        with pytest.raises(ValidationError) as caught:
            services.confirm_suspended_account(
                self._operator(operator),
                held.pk,
                credentials_changed=True,
                factor_removal=services.REJECT,
            )
        assert error_codes(caught.value) == {"factor_removal_contradiction"}
        assert Authenticator.objects.filter(user=held).exists()

    def test_both_questions_are_answered_together_or_not_at_all(
        self, principal: SessionPrincipal, operator: User, people: People
    ) -> None:
        user = people.tech_mad
        old = user.email
        reconcile(
            principal, moved(user, "moved@example.org"), audit_event("user.mfa_reset", user.pk)
        )
        with pytest.raises(ValidationError) as caught:
            services.confirm_suspended_account(
                self._operator(operator), user.pk, email_change=services.ACCEPT
            )
        assert error_codes(caught.value) == {"factor_removal_undecided"}
        user.refresh_from_db()
        assert (user.email, user.is_active) == (old, False), "the email move rolled back"
        confirmed = services.confirm_suspended_account(
            self._operator(operator),
            user.pk,
            email_change=services.ACCEPT,
            factor_removal=services.REJECT,
        )
        assert (confirmed.email, confirmed.is_active) == ("moved@example.org", True)
        assert Authenticator.objects.filter(user=user).exists()


def test_a_grant_keeps_its_row(principal: SessionPrincipal, people: People) -> None:
    """Nothing the replay does deletes a membership: they are revoked, never deleted."""
    before = Membership.objects.count()
    reconcile(principal, audit_event("user.offboarded", people.tech_mad.pk))
    assert Membership.objects.count() == before
    assert Membership.objects.filter(user=people.tech_mad, revoked_at__isnull=False).exists()

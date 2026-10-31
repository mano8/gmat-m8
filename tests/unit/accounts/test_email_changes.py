"""Administrator-initiated email changes (plan §5, F59, F71, F77, Q26, Q28)."""

from collections.abc import Callable
from datetime import timedelta

import pytest
from allauth.account.models import EmailAddress
from django.core.exceptions import ValidationError
from django.utils import timezone

from apps.access.exceptions import AccessDenied, TokenRejected
from apps.access.principals import SessionPrincipal, SystemPrincipal
from apps.access.roles import Role
from apps.accounts import eligibility, services, tokens
from apps.accounts.housekeeping import apply_due_email_changes
from apps.accounts.models import EmailChangeRequest, User
from apps.audit.models import AuditEvent
from apps.notifications.models import EmailOutbox, Status
from apps.notifications.services import run_housekeeping
from apps.organizations.models import Membership
from tests.support import error_codes, grant, principal_for
from tests.tokens import last_token

pytestmark = pytest.mark.django_db

OLD, NEW = "user@example.org", "user-new@example.org"


@pytest.fixture
def user(make_user: Callable[..., User]) -> User:
    return make_user(OLD)


@pytest.fixture
def change(admin_principal: SessionPrincipal, user: User) -> EmailChangeRequest:
    return services.request_email_change(
        admin_principal, user.pk, new_email=" User-New@Example.org "
    )


def confirm(raw: str) -> EmailChangeRequest:
    return services.confirm_email_change(
        tokens.email_change_principal(tokens.hash_token(raw), "confirm")
    )


def cancel(raw: str) -> EmailChangeRequest:
    return services.cancel_email_change(
        tokens.email_change_principal(tokens.hash_token(raw), "cancel")
    )


def cooled_off(item: EmailChangeRequest) -> None:
    EmailChangeRequest.objects.filter(pk=item.pk).update(
        apply_after=timezone.now() - timedelta(seconds=1)
    )


class TestRequest:
    def test_mails_both_addresses(self, change: EmailChangeRequest, user: User) -> None:
        assert change.new_email == NEW and change.requested_by.email == "admin@example.org"
        assert change.expires_at - change.created_at < timedelta(hours=24, seconds=5)
        rules = dict(EmailOutbox.objects.values_list("recipient_address", "eligibility_rule"))
        assert rules == {
            NEW: services.EMAIL_CHANGE_CONFIRMABLE,
            OLD: services.EMAIL_CHANGE_CANCELLABLE,
        }
        assert last_token(NEW) != last_token(OLD)
        assert change.token_hash == tokens.hash_token(last_token(NEW))
        assert change.cancel_token_hash == tokens.hash_token(last_token(OLD))
        user.refresh_from_db()
        assert user.email == OLD  # nothing changes before confirmation and cooling-off

    def test_a_new_request_replaces_the_open_one(
        self, admin_principal: SessionPrincipal, change: EmailChangeRequest, user: User
    ) -> None:
        first = last_token(NEW)
        services.request_email_change(admin_principal, user.pk, new_email="third@example.org")
        change.refresh_from_db()
        assert change.revoke_reason == services.REASON_REPLACED
        with pytest.raises(TokenRejected):
            confirm(first)

    @pytest.mark.parametrize(("address", "code"), [(OLD, "same"), ("admin@example.org", "taken")])
    def test_refused_addresses(
        self, admin_principal: SessionPrincipal, user: User, address: str, code: str
    ) -> None:
        with pytest.raises(ValidationError) as exc:
            services.request_email_change(admin_principal, user.pk, new_email=address)
        assert error_codes(exc) == {code}

    def test_inactive_accounts_are_refused(
        self, admin_principal: SessionPrincipal, make_user: Callable[..., User]
    ) -> None:
        gone = make_user("gone@example.org", is_active=False)
        with pytest.raises(ValidationError) as exc:
            services.request_email_change(admin_principal, gone.pk, new_email="x@example.org")
        assert error_codes(exc) == {"inactive"}

    def test_a_group_admin_cannot_change_a_staff_account(
        self, admin_principal: SessionPrincipal, make_user: Callable[..., User]
    ) -> None:
        staff = make_user("op@example.org", is_staff=True)
        with pytest.raises(AccessDenied):
            services.request_email_change(admin_principal, staff.pk, new_email="x@example.org")
        assert not EmailChangeRequest.objects.exists()

    def test_revoke(self, admin_principal: SessionPrincipal, change: EmailChangeRequest) -> None:
        raw = last_token(NEW)
        services.revoke_email_change(admin_principal, change.pk, reason="typo")
        with pytest.raises(TokenRejected):
            confirm(raw)
        with pytest.raises(ValidationError) as exc:
            services.revoke_email_change(admin_principal, change.pk)
        assert error_codes(exc) == {"closed"}
        assert set(EmailOutbox.objects.values_list("status", flat=True)) == {Status.EXPIRED}


class TestConfirmAndApply:
    def test_confirm_starts_the_cooling_off_then_the_worker_applies(
        self, change: EmailChangeRequest, user: User
    ) -> None:
        raw = last_token(NEW)
        confirmed = confirm(raw)
        assert confirmed.apply_after is not None and confirmed.confirmed_at is not None
        assert confirmed.apply_after - confirmed.confirmed_at == timedelta(hours=24)
        with pytest.raises(TokenRejected, match="already confirmed"):
            confirm(raw)  # single use
        assert apply_due_email_changes() == 0  # still cooling off
        epoch = user.session_epoch
        cooled_off(change)
        done = run_housekeeping(SystemPrincipal())
        assert done["email_changes"] == 1
        user.refresh_from_db()
        assert user.email == NEW and user.session_epoch > epoch
        assert list(EmailAddress.objects.filter(user=user).values_list("email", "verified")) == [
            (NEW, True)
        ]
        change.refresh_from_db()
        assert change.applied_at is not None
        event = AuditEvent.objects.get(action="email_change.applied")
        assert (event.actor_label, event.via) == ("system:worker", "system:worker")

    def test_expired_confirmation(self, change: EmailChangeRequest) -> None:
        EmailChangeRequest.objects.filter(pk=change.pk).update(
            expires_at=timezone.now() - timedelta(seconds=1)
        )
        with pytest.raises(TokenRejected, match="expired"):
            confirm(last_token(NEW))

    def test_the_cancel_token_does_not_confirm(self, change: EmailChangeRequest) -> None:
        with pytest.raises(TokenRejected):
            confirm(last_token(OLD))

    def test_an_initiator_who_lost_org_manage_cannot_be_confirmed_or_applied(
        self, make_user: Callable[..., User], user: User, group_admin: User
    ) -> None:
        other = make_user("other-admin@example.org")
        grant(other, Role.GROUP_ADMIN)
        services.request_email_change(principal_for(other), user.pk, new_email=NEW)
        raw = last_token(NEW)
        # Expiry revokes nothing: the use is refused under the locks instead.
        Membership.objects.filter(user=other).update(
            valid_from=timezone.now() - timedelta(hours=2),
            valid_until=timezone.now() - timedelta(seconds=1),
        )
        with pytest.raises(TokenRejected, match="issuer"):
            confirm(raw)
        # Confirmed while still an administrator, then lost the authority: not applied.
        Membership.objects.filter(user=other).update(valid_until=None)
        confirmed = confirm(raw)
        Membership.objects.filter(user=other).update(
            valid_until=timezone.now() - timedelta(seconds=1)
        )
        cooled_off(confirmed)
        assert apply_due_email_changes() == 0
        confirmed.refresh_from_db()
        assert confirmed.revoke_reason == "initiator lost org.manage"
        user.refresh_from_db()
        assert user.email == OLD

    @pytest.mark.parametrize("problem", ["inactive", "taken", "staff"])
    def test_application_rechecks_everything(
        self, change: EmailChangeRequest, user: User, make_user: Callable[..., User], problem: str
    ) -> None:
        confirm(last_token(NEW))
        if problem == "inactive":
            User.objects.filter(pk=user.pk).update(is_active=False)
        elif problem == "taken":
            make_user(NEW)
        else:
            User.objects.filter(pk=user.pk).update(is_staff=True)
        cooled_off(change)
        assert apply_due_email_changes() == 0
        change.refresh_from_db()
        assert change.revoked_at is not None


class TestCancel:
    def test_the_old_address_cancels_during_the_cooling_off(
        self, change: EmailChangeRequest, user: User
    ) -> None:
        confirm(last_token(NEW))
        raw = last_token(OLD)
        cancelled = cancel(raw)
        assert cancelled.cancelled_at is not None
        cooled_off(change)
        assert apply_due_email_changes() == 0
        user.refresh_from_db()
        assert user.email == OLD
        with pytest.raises(TokenRejected):
            cancel(raw)

    def test_cancel_works_even_after_the_initiator_left(
        self, make_user: Callable[..., User], user: User
    ) -> None:
        other = make_user("other-admin@example.org")
        grant(other, Role.GROUP_ADMIN)
        services.request_email_change(principal_for(other), user.pk, new_email=NEW)
        Membership.objects.filter(user=other).update(revoked_at=timezone.now())
        assert cancel(last_token(OLD)).cancelled_at is not None


def test_mail_rules(change: EmailChangeRequest) -> None:
    confirm_mail = EmailOutbox.objects.get(recipient_address=NEW)
    notice = EmailOutbox.objects.get(recipient_address=OLD)
    assert eligibility.email_change_confirmable(confirm_mail)
    assert eligibility.email_change_cancellable(notice)
    confirm(last_token(NEW))
    assert not eligibility.email_change_confirmable(confirm_mail)
    assert eligibility.email_change_cancellable(notice)
    notice.eligibility_object_id = None
    assert not eligibility.email_change_cancellable(notice)


@pytest.mark.parametrize("problem", ["inactive", "taken"])
def test_confirmation_needs_an_active_account_and_a_free_address(
    change: EmailChangeRequest, user: User, make_user: Callable[..., User], problem: str
) -> None:
    if problem == "inactive":
        User.objects.filter(pk=user.pk).update(is_active=False)
    else:
        make_user(NEW)
    change.refresh_from_db()
    assert tokens.email_change_problem(change, "confirm") is not None
    assert tokens.email_change_problem(change, "cancel") is None

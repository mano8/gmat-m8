"""Recipient eligibility at the database clock of the claim (plan §5, F42, F53)."""

from collections.abc import Callable
from datetime import timedelta

import pytest
from django.utils import timezone

from apps.access.roles import Capability
from apps.accounts.models import User
from apps.notifications import eligibility
from apps.notifications.models import EmailOutbox
from apps.organizations.models import Membership
from apps.projects.models import Project
from tests.org import Org

pytestmark = pytest.mark.django_db


class TestNotificationRecipients:
    def test_only_recipients_who_still_hold_the_capability_on_the_object(
        self,
        notification: Callable[..., EmailOutbox],
        manager: User,
        outsider: User,
        make_user: Callable[..., User],
    ) -> None:
        nobody = make_user("nobody@example.org")
        message = notification(manager, outsider, nobody)
        assert eligibility.eligible_recipients(message) == [manager]

    def test_a_revoked_expired_or_inactive_recipient_is_dropped(
        self,
        notification: Callable[..., EmailOutbox],
        manager: User,
        org: Org,
        member: Callable[..., User],
    ) -> None:
        from apps.access.roles import Role

        expiring = member(Role.MATERIAL_MANAGER, email="exp@example.org", branch=org.a_mad)
        leaving = member(Role.MATERIAL_MANAGER, email="leave@example.org", branch=org.a_mad)
        message = notification(manager, expiring, leaving)
        Membership.objects.filter(user=manager).update(revoked_at=timezone.now())
        Membership.objects.filter(user=expiring).update(
            valid_until=timezone.now() - timedelta(minutes=1)
        )
        User.objects.filter(pk=leaving.pk).update(is_active=False)
        assert eligibility.eligible_recipients(message) == []

    def test_offboarded_recipients_are_dropped(
        self, notification: Callable[..., EmailOutbox], manager: User
    ) -> None:
        message = notification(manager)
        User.objects.filter(pk=manager.pk).update(offboarded_at=timezone.now())
        assert eligibility.eligible_recipients(message) == []

    def test_without_an_object_the_capability_anywhere_is_enough(
        self, notification: Callable[..., EmailOutbox], manager: User, account: User
    ) -> None:
        message = notification(manager, account, obj=None)
        message.eligibility_object_type, message.eligibility_object_id = "", None
        assert eligibility.eligible_recipients(message) == [manager]

    def test_an_object_that_is_gone_or_unknown_makes_nobody_eligible(
        self, notification: Callable[..., EmailOutbox], manager: User, account: User
    ) -> None:
        message = notification(manager)
        for label, pk in [
            ("projects.project", account.pk),  # no such project
            ("nosuch.model", message.eligibility_object_id),
            ("not-a-label", message.eligibility_object_id),
            ("accounts.user", account.pk),  # exists, but has no scoped queryset
        ]:
            message.eligibility_object_type, message.eligibility_object_id = label, pk
            assert eligibility.eligible_recipients(message) == [], label

    def test_nothing_to_load_without_an_id(self) -> None:
        assert eligibility._load("projects.project", None) is None

    def test_a_project_put_on_hold_still_reaches_its_managers(
        self, notification: Callable[..., EmailOutbox], manager: User, org: Org
    ) -> None:
        message = notification(manager)
        Project.objects.filter(pk=org.hospital.pk).update(status=Project.Status.ON_HOLD)
        assert eligibility.eligible_recipients(message) == [manager]

    def test_capability_is_the_rule(
        self, notification: Callable[..., EmailOutbox], manager: User
    ) -> None:
        message = notification(manager, capability=Capability.ORG_MANAGE)
        assert eligibility.eligible_recipients(message) == []


class TestSecurityRules:
    def test_account_active_follows_the_target_account(
        self, security_mail: Callable[..., EmailOutbox], account: User
    ) -> None:
        message = security_mail()
        assert eligibility.security_target_ok(message)
        User.objects.filter(pk=account.pk).update(email="changed@example.org")
        assert not eligibility.security_target_ok(message)
        User.objects.filter(pk=account.pk).update(email=account.email, is_active=False)
        assert not eligibility.security_target_ok(message)
        User.objects.filter(pk=account.pk).update(is_active=True, offboarded_at=timezone.now())
        assert not eligibility.security_target_ok(message)

    def test_the_target_must_be_an_account(
        self, security_mail: Callable[..., EmailOutbox], org: Org
    ) -> None:
        message = security_mail(target=org.hospital)
        assert not eligibility.security_target_ok(message)
        message.eligibility_object_type = "accounts.user"
        assert not eligibility.security_target_ok(message)  # no account with that id

    def test_an_unregistered_rule_is_never_eligible(
        self, security_mail: Callable[..., EmailOutbox]
    ) -> None:
        message = security_mail()
        message.eligibility_rule = "gone.rule"
        assert not eligibility.security_target_ok(message)

    def test_a_rule_name_is_registered_once(self) -> None:
        eligibility.register_security_rule(eligibility.ACCOUNT_ACTIVE, eligibility._account_active)
        with pytest.raises(ValueError, match="already registered"):
            eligibility.register_security_rule(eligibility.ACCOUNT_ACTIVE, lambda message: True)
        assert eligibility.is_registered(eligibility.ACCOUNT_ACTIVE)

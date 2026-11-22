"""The user's own choice about business email (M7.1): service, selector and screen."""

import pytest

from apps.access.context import AccessContext
from apps.access.exceptions import AccessDenied
from apps.audit.models import AuditEvent
from apps.notifications import selectors, services
from apps.notifications.models import NotificationPreference
from tests.org import People
from tests.support import principal_for

pytestmark = pytest.mark.django_db

ACTION = "notification.own_changed"


class TestService:
    def test_opting_out_and_back_in_is_audited(self, people: People) -> None:
        principal = principal_for(people.tech_mad)
        services.set_email_opt_out(principal, True)
        assert NotificationPreference.objects.get(user=people.tech_mad).email_opt_out is True
        services.set_email_opt_out(principal, False)
        assert NotificationPreference.objects.get(user=people.tech_mad).email_opt_out is False
        assert AuditEvent.objects.filter(action=ACTION).count() == 2

    def test_a_change_that_alters_nothing_writes_nothing(self, people: People) -> None:
        principal = principal_for(people.tech_mad)
        services.set_email_opt_out(principal, False)  # the default
        services.set_email_opt_out(principal, False)
        services.set_email_opt_out(principal, True)
        services.set_email_opt_out(principal, True)
        assert AuditEvent.objects.filter(action=ACTION).count() == 1

    def test_only_the_principals_own_row_changes(self, people: People) -> None:
        services.set_email_opt_out(principal_for(people.tech_mad), True)
        assert not NotificationPreference.objects.filter(user=people.mm_mad).exists()

    def test_an_account_without_a_membership_is_refused(self, people: People) -> None:
        with pytest.raises(AccessDenied):
            services.set_email_opt_out(principal_for(people.nobody), True)

    def test_a_deactivated_account_is_refused(self, people: People) -> None:
        with pytest.raises(AccessDenied):
            services.set_email_opt_out(principal_for(people.deactivated), True)


def test_selector_reads_the_own_channels(people: People) -> None:
    ctx = AccessContext.for_user(people.tech_mad)
    assert [state.enabled for state in selectors.own_channels(ctx)] == [True, True]
    services.set_email_opt_out(principal_for(people.tech_mad), True)
    assert [state.enabled for state in selectors.own_channels(ctx)] == [False, True]


def test_selector_for_an_anonymous_visitor() -> None:
    anonymous = AccessContext.anonymous()
    assert selectors.own_channels(anonymous) == []
    assert selectors.own_notices(anonymous) == []

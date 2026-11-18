"""Two administrators changing the same setting at once end with one row and two audited
changes, never an ``IntegrityError`` (M15.2). The person's user row serializes the changes to
their settings; the first decision for everyone is a ``get_or_create`` on a unique key."""

from collections.abc import Callable
from typing import Any

import pytest

from apps.access.principals import SessionPrincipal
from apps.access.roles import Role
from apps.accounts.models import User
from apps.audit.models import AuditEvent
from apps.notifications import services
from apps.notifications.kinds import Mode, Notice
from apps.notifications.models import NoticeDefault, NoticeSetting
from tests.integration.conftest import run_concurrently
from tests.support import grant, principal_for

pytestmark = [pytest.mark.integration, pytest.mark.serial, pytest.mark.django_db(transaction=True)]


@pytest.fixture
def admins(make_user: Callable[..., User]) -> list[SessionPrincipal]:
    made = []
    for name in ("one", "two"):
        admin = make_user(f"admin-{name}@example.org")
        grant(admin, Role.GROUP_ADMIN)
        made.append(principal_for(admin))
    return made


def test_two_first_decisions_for_everyone(admins: list[SessionPrincipal]) -> None:
    results: list[Any] = run_concurrently(
        2,
        lambda index: services.set_notice_default(
            admins[index], Notice.REQUEST_APPROVED, enabled=bool(index)
        ),
    )
    assert all(isinstance(result, NoticeDefault) for result in results), results
    assert NoticeDefault.objects.count() == 1
    assert AuditEvent.objects.filter(action="notification.default_changed").count() >= 1


def test_two_settings_of_one_person(
    admins: list[SessionPrincipal], make_user: Callable[..., User]
) -> None:
    person = make_user("person@example.org")
    modes = (Mode.ALWAYS, Mode.NEVER)
    results: list[Any] = run_concurrently(
        2,
        lambda index: services.set_person_notices(
            admins[index], person.pk, {Notice.REQUEST_TO_REVIEW: modes[index]}
        ),
    )
    assert all(isinstance(result, User) for result in results), results
    assert NoticeSetting.objects.filter(user=person).count() == 1
    assert AuditEvent.objects.filter(action="notification.person_changed").count() == 2

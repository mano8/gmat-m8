"""Two changes that are each valid alone cannot combine into two ``managed_by`` levels.

A-SEV → A-MAD and A-MAD → A-HQ are each fine alone, but together A-SEV would be managed by
a managed branch. Run concurrently, exactly one commits: through the services (serialized
by the ``AccessState`` lock, the second sees the first in ``clean``) and through plain ORM
updates (serialized by the trigger's advisory lock).
"""

import time
from collections.abc import Callable
from typing import Any

import pytest
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction

from apps.access.roles import Role
from apps.accounts.models import User
from apps.core import events
from apps.organizations.models import Branch, Company
from apps.organizations.services import update_branch
from tests.integration.conftest import run_concurrently
from tests.support import grant, principal_for

pytestmark = [pytest.mark.integration, pytest.mark.django_db(transaction=True)]


@pytest.fixture
def branches() -> tuple[Branch, Branch, Branch]:
    company = Company.objects.create(code="A", legal_name="A")
    return (
        Branch.objects.create(company=company, code="HQ", name="HQ"),
        Branch.objects.create(company=company, code="MAD", name="Madrid"),
        Branch.objects.create(company=company, code="SEV", name="Sevilla"),
    )


def outcome(results: list[Any]) -> list[str]:
    return sorted(type(result).__name__ for result in results)


def two_levels() -> bool:
    return Branch.objects.filter(managed_by__managed_by__isnull=False).exists()


def test_services(make_user: Callable[..., User], branches: tuple[Branch, Branch, Branch]) -> None:
    hq, mad, sev = branches
    admin = make_user("admin@example.org")
    grant(admin, Role.GROUP_ADMIN)
    principal = principal_for(admin)
    changes = [(sev.pk, mad.pk), (mad.pk, hq.pk)]

    def slow(event: events.Event) -> None:
        time.sleep(0.3)  # the other change arrives while this one holds its locks

    undo = events.subscribe("branch.updated", slow)
    try:
        results = run_concurrently(
            2,
            lambda index: update_branch(
                principal, changes[index][0], managed_by_id=changes[index][1]
            ),
        )
    finally:
        undo()
    assert outcome(results) == ["Branch", "ValidationError"]
    assert next(r for r in results if isinstance(r, ValidationError)).error_dict["managed_by"]
    assert not two_levels()


def test_plain_updates(branches: tuple[Branch, Branch, Branch]) -> None:
    hq, mad, sev = branches
    changes = [(sev.pk, mad.pk), (mad.pk, hq.pk)]

    def run(index: int) -> int:
        branch_id, manager_id = changes[index]
        with transaction.atomic():
            updated = Branch.objects.filter(pk=branch_id).update(managed_by_id=manager_id)
            time.sleep(0.3)
            return updated

    results = run_concurrently(2, run)
    assert outcome(results) == ["IntegrityError", "int"]
    assert isinstance(next(r for r in results if not isinstance(r, int)), IntegrityError)
    assert not two_levels()

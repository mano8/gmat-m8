"""Fixtures of the outbox tests: the worker's principal, messages, a fake dispatch source."""

from collections.abc import Callable, Iterator
from typing import Any

import pytest
from django.db import transaction

from apps.access.principals import SystemPrincipal
from apps.access.roles import Capability, Role
from apps.accounts.models import User
from apps.notifications.dispatch import register_dispatch_source
from apps.notifications.eligibility import ACCOUNT_ACTIVE
from apps.notifications.models import EmailOutbox, Kind
from apps.notifications.outbox import queue_dispatch, queue_notification, queue_security_email
from apps.projects.models import Project
from tests.org import Org
from tests.outbox import SECRET_BODY, FakeOrders


@pytest.fixture
def system() -> SystemPrincipal:
    return SystemPrincipal()


@pytest.fixture
def account(make_user: Callable[..., User]) -> User:
    return make_user("reset-me@example.org")


@pytest.fixture
def security_mail(account: User) -> Callable[..., EmailOutbox]:
    def make(user: User | None = None, **extra: Any) -> EmailOutbox:
        target = user or account
        values: dict[str, Any] = {
            "address": target.email,
            "target": target,
            "rule": ACCOUNT_ACTIVE,
            "subject": "Password reset",
            "body": SECRET_BODY,
        }
        return queue_security_email(**(values | extra))

    return make


@pytest.fixture
def manager(org: Org, member: Callable[..., User]) -> User:
    """A material manager of A-MAD: sees the hospital project (A-MAD manages B-MAD)."""
    return member(Role.MATERIAL_MANAGER, email="mm@example.org", branch=org.a_mad)


@pytest.fixture
def outsider(org: Org, member: Callable[..., User]) -> User:
    """A material manager of A-SEV: does not see the hospital project."""
    return member(Role.MATERIAL_MANAGER, email="mm-sev@example.org", branch=org.a_sev)


@pytest.fixture
def notification(org: Org) -> Callable[..., EmailOutbox]:
    def make(*users: User, obj: Project | None = None, **extra: Any) -> EmailOutbox:
        values: dict[str, Any] = {
            "recipients": [user.pk for user in users],
            "capability": Capability.PROJECT_VIEW,
            "obj": org.hospital if obj is None else obj,
            "subject": "Project news",
            "body": "Something happened.",
        }
        with transaction.atomic():
            (message,) = queue_notification(**(values | extra))
        return message

    return make


@pytest.fixture
def orders() -> Iterator[FakeOrders]:
    source = FakeOrders()
    unregister = register_dispatch_source(source)
    yield source
    unregister()


@pytest.fixture
def dispatch(org: Org) -> Callable[..., EmailOutbox]:
    def make(kind: Kind = Kind.PO_ORDER, po: Project | None = None) -> EmailOutbox:
        with transaction.atomic():
            return queue_dispatch(
                kind=kind,
                po_id=(po or org.hospital).pk,
                address="orders@supplier.example",
                subject="Purchase order PED-A-2026-00001",
                body="Please deliver.",
                source_ref="PO:test:rev1",
            )

    return make

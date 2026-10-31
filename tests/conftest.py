"""Shared pytest fixtures.

The hermetic environment (database defaults) is seeded in ``config.settings.test``,
because pytest-django imports the settings before this file is loaded. Plain helpers
live in ``tests/support.py``.
"""

import importlib.util
from collections.abc import Callable, Iterator
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
from django.test import override_settings

from apps.access import principals
from apps.access.principals import SessionPrincipal
from apps.access.roles import Role
from apps.accounts.models import User
from apps.organizations.models import Branch, Company
from tests.org import Org, People, build_org, build_people
from tests.org import member as org_member
from tests.support import PASSWORD, TOTP_COUNTER, grant, principal_for

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"


@pytest.fixture(autouse=True, scope="session")
def _tests_may_construct_principals() -> Iterator[None]:
    """Tests build token, system and operator principals directly.

    Production code may only construct them in their allowlisted modules; ``tests/`` is
    not part of any image (``.dockerignore``).
    """
    with pytest.MonkeyPatch.context() as patch:
        for name, modules in list(principals.CONSTRUCTOR_MODULES.items()):
            patch.setitem(principals.CONSTRUCTOR_MODULES, name, modules | {"tests"})
        yield


@pytest.fixture(autouse=True, scope="session")
def _mail_goes_through_the_outbox(django_test_environment: None) -> Iterator[None]:
    """Requests queue mail in the outbox, as in production.

    Django's test runner replaces every mailer with locmem; this puts the outbox back as the
    default mailer. The worker's ``outbound`` mailer stays locmem (``django.core.mail.outbox``).
    """
    mailers = {
        "default": {"BACKEND": "apps.notifications.backends.OutboxEmailBackend"},
        "outbound": {"BACKEND": "django.core.mail.backends.locmem.EmailBackend"},
    }
    with override_settings(MAILERS=mailers):
        yield


@pytest.fixture(scope="session")
def load_script() -> Callable[[str], ModuleType]:
    """Import a standalone script from ``scripts/`` as a module."""

    def load(name: str) -> ModuleType:
        spec = importlib.util.spec_from_file_location(name, SCRIPTS / f"{name}.py")
        assert spec and spec.loader
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    return load


@pytest.fixture
def make_user(db: None) -> Callable[..., User]:
    def make(email: str = "tech@example.org", password: str = PASSWORD, **extra: Any) -> User:
        return User.objects.create_user(email, password, **extra)

    return make


@pytest.fixture
def org(db: None) -> Org:
    """The A/B/C organization of plan §3.2 (``tests/org.py``)."""
    return build_org()


@pytest.fixture
def people(org: Org) -> People:
    """One user per role and the mixed, expired, revoked and inactive cases (plan §8.1)."""
    return build_people(org)


@pytest.fixture
def member(db: None) -> Callable[..., User]:
    """A user with one membership: ``member(Role.X, branch=…, company=…, **validity)``."""
    count = iter(range(1000))

    def make(role: Role, *, email: str | None = None, **kwargs: Any) -> User:
        return org_member(
            role, email=email or f"{role.lower()}-{next(count)}@example.org", **kwargs
        )

    return make


@pytest.fixture
def frozen_totp(monkeypatch: pytest.MonkeyPatch) -> int:
    """Pin allauth's TOTP clock to one time step."""
    monkeypatch.setattr(
        "allauth.mfa.totp.internal.auth.yield_hotp_counters_from_time",
        lambda: iter([TOTP_COUNTER]),
    )
    return TOTP_COUNTER


@pytest.fixture
def group_admin(make_user: Callable[..., User]) -> User:
    """A user with a valid ``GROUP_ADMIN`` membership (``org.manage``, ``project.manage``)."""
    user = make_user("admin@example.org")
    grant(user, Role.GROUP_ADMIN)
    return user


@pytest.fixture
def admin_principal(group_admin: User) -> SessionPrincipal:
    return principal_for(group_admin)


@pytest.fixture
def company(admin_principal: SessionPrincipal) -> Company:
    """Company ``A`` with its automatic head-office branch ``HQ``."""
    from apps.organizations.services import create_company

    return create_company(admin_principal, code="A", legal_name="Company A")


@pytest.fixture
def branch(admin_principal: SessionPrincipal, company: Company) -> Branch:
    """Branch ``A-MAD`` of company ``A``."""
    from apps.organizations.services import create_branch

    return create_branch(
        admin_principal, company_id=company.pk, code="MAD", name="Madrid", city="Madrid"
    )

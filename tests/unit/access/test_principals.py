"""Principals and their restricted constructors (plan §3.6)."""

import time
from collections.abc import Callable
from typing import Any
from uuid import UUID

import pytest
from django.contrib.auth.models import AnonymousUser
from django.test import Client, RequestFactory

from apps.access import principals
from apps.access.exceptions import PrincipalNotAllowed
from apps.access.principals import (
    OperatorPrincipal,
    SessionPrincipal,
    SystemPrincipal,
    TokenKind,
    TokenPrincipal,
)
from apps.accounts.models import User
from apps.accounts.sessions import SESSION_EPOCH_KEY
from apps.audit.recorder import AuditIdentity
from tests.support import enroll_totp, mfa_login

pytestmark = pytest.mark.django_db

CONSTRUCTORS = {
    "TokenPrincipal": (
        "TokenPrincipal(kind=TokenKind.PASSWORD_RESET, operation='x', target_user_id=None,"
        " target_email='a@example.org', check_token=lambda target, locks: None)"
    ),
    "SystemPrincipal": "SystemPrincipal()",
    "OperatorPrincipal": (
        "OperatorPrincipal(user_id=None, email='a@example.org', command='c', capability='x')"
    ),
}


def construct_in(module: str, class_name: str) -> Any:
    """Run the constructor as code of ``module`` (its frame's ``__name__``)."""
    namespace = {**vars(principals), "__name__": module}
    return eval(CONSTRUCTORS[class_name], namespace)  # noqa: S307  # nosec B307 - fixed text


class TestRestrictedConstructors:
    def test_the_allowlist_is_the_plans(self) -> None:
        assert (
            {
                "TokenPrincipal": {"apps.accounts.tokens", "apps.accounts.adapters", "tests"},
                "SystemPrincipal": {"apps.notifications.worker", "tests"},
                "OperatorPrincipal": {"apps.core.commands", "tests"},
            }
            == principals.CONSTRUCTOR_MODULES
        )  # "tests" is added by tests/conftest.py, never in production

    @pytest.mark.parametrize(
        ("class_name", "module"),
        [
            ("TokenPrincipal", "apps.accounts.tokens"),
            ("TokenPrincipal", "apps.accounts.adapters"),
            ("SystemPrincipal", "apps.notifications.worker"),
            ("SystemPrincipal", "apps.notifications.worker.claims"),
            ("OperatorPrincipal", "apps.core.commands"),
        ],
    )
    def test_allowed_in_their_modules(self, class_name: str, module: str) -> None:
        assert type(construct_in(module, class_name)).__name__ == class_name

    @pytest.mark.parametrize("class_name", sorted(CONSTRUCTORS))
    @pytest.mark.parametrize(
        "module",
        ["apps.core.views", "apps.organizations.services", "apps.notifications.workers", ""],
    )
    def test_refused_anywhere_else(self, class_name: str, module: str) -> None:
        with pytest.raises(PrincipalNotAllowed, match=class_name):
            construct_in(module, class_name)

    def test_a_session_principal_is_built_anywhere(self) -> None:
        namespace = {**vars(principals), "__name__": "apps.core.views"}
        built = eval("SessionPrincipal(user_id=None, email='a@example.org')", namespace)  # noqa: S307  # nosec B307
        assert isinstance(built, SessionPrincipal)

    def test_only_known_system_actors(self) -> None:
        with pytest.raises(ValueError, match="system actor"):
            SystemPrincipal(name="system:other")


class TestSessionPrincipal:
    def test_from_a_request_with_the_sessions_epoch_and_mfa_time(
        self, client: Client, make_user: Callable[..., User], frozen_totp: int
    ) -> None:
        user = make_user()
        mfa_login(client, user.email, enroll_totp(user))
        request = RequestFactory().get("/", REMOTE_ADDR="10.1.2.3")
        request.user = user
        request.session = client.session
        principal = SessionPrincipal.from_request(request)
        assert (principal.user_id, principal.email, principal.ip) == (
            user.pk,
            user.email,
            "10.1.2.3",
        )
        assert principal.session_epoch == client.session[SESSION_EPOCH_KEY] == 0
        assert principal.mfa_at is not None and principal.mfa_at <= time.time()
        assert principal.audit_identity == AuditIdentity(
            label=user.email, user_id=user.pk, ip="10.1.2.3"
        )

    def test_a_session_without_an_epoch_carries_none(
        self, client: Client, make_user: Callable[..., User]
    ) -> None:
        user = make_user()
        client.force_login(user)
        session = client.session
        session[SESSION_EPOCH_KEY] = "tampered"
        request = RequestFactory().get("/")
        request.user = user
        request.session = session
        principal = SessionPrincipal.from_request(request)
        assert principal.session_epoch is None
        assert principal.mfa_at is None

    def test_needs_an_authenticated_user(self) -> None:
        request = RequestFactory().get("/")
        request.user = AnonymousUser()
        with pytest.raises(ValueError, match="authenticated"):
            SessionPrincipal.from_request(request)


class TestAuditIdentities:
    @pytest.mark.parametrize(
        ("kind", "via"),
        [
            (TokenKind.INVITATION, "invitation:00000000-0000-0000-0000-000000000007"),
            (TokenKind.EMAIL_CHANGE, "email_change:00000000-0000-0000-0000-000000000007"),
            (TokenKind.BOOTSTRAP, "bootstrap"),
            (TokenKind.PASSWORD_RESET, "password_reset"),
        ],
    )
    def test_token_principals_act_as_their_target_via_the_token(
        self, kind: TokenKind, via: str
    ) -> None:
        target = UUID(int=3)
        token = TokenPrincipal(
            kind=kind,
            operation="x",
            target_user_id=target,
            target_email="t@example.org",
            check_token=lambda found, locks: None,
            token_id=UUID(int=7),
            ip="10.0.0.1",
        )
        assert token.audit_identity == AuditIdentity(
            label="t@example.org", user_id=target, via=via, ip="10.0.0.1"
        )
        assert "check_token" not in repr(token)

    def test_the_worker(self) -> None:
        assert SystemPrincipal().audit_identity == AuditIdentity(
            label="system:worker", via="system:worker"
        )

    def test_operators_act_via_the_command(self) -> None:
        operator = OperatorPrincipal(
            user_id=UUID(int=4), email="o@example.org", command="import_catalog", capability="x"
        )
        assert operator.audit_identity == AuditIdentity(
            label="o@example.org", user_id=UUID(int=4), via="cli:import_catalog"
        )

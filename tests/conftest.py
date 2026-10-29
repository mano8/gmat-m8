"""Shared pytest fixtures.

The hermetic environment (database defaults) is seeded in ``config.settings.test``,
because pytest-django imports the settings before this file is loaded. Plain helpers
live in ``tests/support.py``.
"""

import importlib.util
from collections.abc import Callable
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

from apps.accounts.models import User
from tests.support import PASSWORD, TOTP_COUNTER

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"


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
def frozen_totp(monkeypatch: pytest.MonkeyPatch) -> int:
    """Pin allauth's TOTP clock to one time step."""
    monkeypatch.setattr(
        "allauth.mfa.totp.internal.auth.yield_hotp_counters_from_time",
        lambda: iter([TOTP_COUNTER]),
    )
    return TOTP_COUNTER

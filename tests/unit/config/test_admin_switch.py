"""``DJANGO_ADMIN_ENABLED`` (ADR-0030): off, the admin has no URL; its checks still run."""

import importlib
from collections.abc import Callable, Iterator

import pytest
from django.contrib import admin
from django.test import Client, override_settings
from django.urls import NoReverseMatch, clear_url_caches, reverse

import config.urls
from apps.accounts.models import User
from apps.core import checks
from tests.support import enroll_totp, mfa_login

pytestmark = pytest.mark.django_db


def _reload_urls() -> None:
    importlib.reload(config.urls)
    clear_url_caches()


@pytest.fixture
def admin_off() -> Iterator[None]:
    with override_settings(ADMIN_ENABLED=False):
        _reload_urls()
        yield
    _reload_urls()


@pytest.fixture
def admitted(client: Client, make_user: Callable[..., User], frozen_totp: int) -> Client:
    """A platform operator whose session passed MFA: the admin would let them in."""
    operator = make_user("ops@example.org", is_staff=True, is_superuser=True)
    mfa_login(client, operator.email, enroll_totp(operator))
    return client


@pytest.mark.usefixtures("admin_off")
class TestOff:
    def test_the_prefix_is_an_unknown_path(self, admitted: Client) -> None:
        unknown = admitted.get("/no-such-page/")
        for path in ("/admin/", "/admin/login/", "/admin/organizations/company/"):
            response = admitted.get(path)
            assert response.status_code == 404, path
            assert [t.name for t in response.templates] == [t.name for t in unknown.templates]
        assert Client().get("/admin/").status_code == 404

    def test_nothing_reverses_to_it(self) -> None:
        with pytest.raises(NoReverseMatch):
            reverse("admin:index")

    def test_its_checks_still_run(self) -> None:
        assert admin.site._registry
        assert checks.check_admin_base_class(None) == []
        assert checks.check_admin_read_only(None) == []


def test_on_it_is_served(admitted: Client) -> None:
    assert admitted.get(reverse("admin:index")).status_code == 200

"""The first administrator, from an empty database (plan §5, F76, Q29; M2.6 done-when).

``bootstrap_admin`` → setup link → password and MFA → the new ``GROUP_ADMIN`` manages
memberships in the app and runs an ``--operator`` command, with no manual ORM edit; a second
``bootstrap_admin`` is refused.
"""

import re
from io import StringIO
from typing import Any
from urllib.parse import urlsplit

import pytest
from allauth.mfa.totp.internal.auth import SECRET_SESSION_KEY
from django.core.management import CommandError, call_command
from django.core.management.base import BaseCommand
from django.test import Client
from django.urls import reverse

from apps.access.principals import OperatorPrincipal
from apps.access.roles import Capability, Role
from apps.accounts.models import Invitation, User
from apps.audit.models import AuditEvent
from apps.core.commands import OperatorCommand
from apps.organizations.models import Branch, Company, Membership
from tests.support import password_login, totp_code

pytestmark = pytest.mark.django_db

PASSWORD = "first-admin-password-1"
LINK = re.compile(r"^(https?://\S+/invite/\S+/)$", re.MULTILINE)


class CountCompanies(OperatorCommand):
    """Stands in for an import (M3.3): runs on ``--operator``'s ``org.manage``."""

    required_capability = Capability.ORG_MANAGE

    def handle_operator(self, principal: OperatorPrincipal, *args: Any, **options: Any) -> None:
        self.stdout.write(f"{principal.email}:{Company.objects.count()}")


def bootstrap(*args: str) -> str:
    out = StringIO()
    call_command("bootstrap_admin", *args, stdout=out)
    return out.getvalue()


def setup_link(output: str) -> str:
    match = LINK.search(output)
    assert match is not None, output
    return urlsplit(match.group(1)).path


def test_from_an_empty_database(frozen_totp: int, monkeypatch: pytest.MonkeyPatch) -> None:
    assert not User.objects.exists() and not Company.objects.exists()
    output = bootstrap(
        "--email", "Boss@Example.org", "--company-code", "a", "--company-name", "Company A"
    )
    company = Company.objects.get()
    assert (company.code, company.legal_name) == ("A", "Company A")
    assert Branch.objects.get(company=company).code == "HQ"
    invitation = Invitation.objects.get()
    assert (invitation.email, invitation.bootstrap, invitation.invited_by) == (
        "boss@example.org",
        True,
        None,
    )
    assert AuditEvent.objects.filter(action="invitation.created", via="bootstrap").exists()

    client = Client()
    # The link: its token goes into the session, never into the next URL.
    response = client.get(setup_link(output))
    assert response.status_code == 302
    assert response["Location"] == reverse("invitations:accept")
    assert response["Referrer-Policy"] == "no-referrer"
    assert "no-store" in response["Cache-Control"]
    form = client.get(response["Location"])
    assert form.status_code == 200 and b"password1" in form.content
    accepted = client.post(
        reverse("invitations:submit"), {"password1": PASSWORD, "password2": PASSWORD}
    )
    assert accepted.status_code == 302 and accepted["Location"] == reverse("account_login")
    admin = User.objects.get(email="boss@example.org")
    assert Membership.objects.get(user=admin).role == Role.GROUP_ADMIN
    assert admin.employer_company == company and not admin.is_superuser

    # First sign-in: a privileged account enrolls MFA before anything else.
    assert password_login(client, admin.email, PASSWORD).status_code == 302
    home = client.get(reverse("core:home"))
    assert home.status_code == 302 and home["Location"].startswith(reverse("mfa_index"))
    activate = client.get(reverse("mfa_activate_totp"))
    assert activate["Location"].startswith(reverse("account_reauthenticate"))
    client.post(reverse("account_reauthenticate"), {"password": PASSWORD})
    assert client.get(reverse("mfa_activate_totp")).status_code == 200
    secret = client.session[SECRET_SESSION_KEY]
    enrolled = client.post(reverse("mfa_activate_totp"), {"code": totp_code(secret)})
    assert enrolled.status_code == 302
    # Enrolling is not completing MFA: the session steps up, with the next time step's code
    # (allauth refuses a code twice).
    step_up = client.get(reverse("org:people"))
    assert step_up["Location"].startswith(reverse("mfa_reauthenticate"))
    monkeypatch.setattr(
        "allauth.mfa.totp.internal.auth.yield_hotp_counters_from_time",
        lambda: iter([frozen_totp + 1]),
    )
    client.post(reverse("mfa_reauthenticate"), {"code": totp_code(secret, frozen_totp + 1)})
    assert client.get(reverse("org:people")).status_code == 200

    # The new administrator manages memberships in the app.
    worker = User.objects.create_user("worker@example.org")
    person = client.get(reverse("org:person", args=[worker.pk]))
    key = re.search(rb'name="idempotency_key" value="([^"]+)"', person.content)
    assert key is not None
    granted = client.post(
        reverse("org:grant_membership", args=[worker.pk]),
        {
            "role": Role.TECHNICIAN,
            "branch": Branch.objects.get().pk,
            "idempotency_key": key.group(1).decode(),
        },
    )
    assert granted.status_code == 302
    assert Membership.objects.get(user=worker).role == Role.TECHNICIAN

    # And runs a command on their own authority.
    out = StringIO()
    command: BaseCommand = CountCompanies(stdout=out)
    call_command(command, "--operator", admin.email)
    assert out.getvalue().strip() == "boss@example.org:1"

    # A second bootstrap is refused.
    with pytest.raises(CommandError, match="already exists"):
        bootstrap("--email", "other@example.org", "--company-code", "B")
    assert Invitation.objects.count() == 1 and Company.objects.count() == 1


def test_running_it_again_replaces_the_link_while_nobody_accepted() -> None:
    first = setup_link(bootstrap("--email", "boss@example.org", "--company-code", "A"))
    second = setup_link(bootstrap("--email", "boss@example.org", "--company-code", "A"))
    assert first != second and Company.objects.count() == 1
    client = Client()
    client.get(first)
    assert b"password1" not in client.get(reverse("invitations:accept")).content
    client.get(second)
    assert b"password1" in client.get(reverse("invitations:accept")).content


def test_a_link_issued_before_an_administrator_existed_dies(make_user: Any) -> None:
    link = setup_link(bootstrap("--email", "boss@example.org", "--company-code", "A"))
    other = make_user("admin@example.org")
    Membership.objects.create(user=other, role=Role.GROUP_ADMIN, valid_from="2000-01-01T00:00Z")
    client = Client()
    client.get(link)
    response = client.post(
        reverse("invitations:submit"), {"password1": PASSWORD, "password2": PASSWORD}
    )
    assert response.status_code == 200 and not User.objects.filter(email="boss@example.org")


def test_superuser_variant() -> None:
    link = setup_link(
        bootstrap("--email", "root@example.org", "--company-code", "A", "--superuser")
    )
    client = Client()
    client.get(link)
    client.post(reverse("invitations:submit"), {"password1": PASSWORD, "password2": PASSWORD})
    root = User.objects.get(email="root@example.org")
    assert root.is_staff and root.is_superuser
    with pytest.raises(CommandError, match="superuser"):
        bootstrap("--email", "x@example.org", "--company-code", "A", "--superuser")


@pytest.mark.parametrize("setup", ["existing-account", "inactive-company"])
def test_refusals(setup: str) -> None:
    if setup == "existing-account":
        User.objects.create_user("boss@example.org")
    else:
        Company.objects.create(code="A", legal_name="A", is_active=False)
    with pytest.raises(CommandError):
        bootstrap("--email", "boss@example.org", "--company-code", "A")
    assert not Invitation.objects.exists()

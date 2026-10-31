"""The shared fixture of plan §8.1: the A/B/C organization, its people and sentinel values.

Built directly with the ORM (triggers still run), so the tests do not depend on the
services they protect. ``tests/conftest.py`` exposes ``org``, ``member`` and ``people`` per
test; ``tests/access`` builds the same world once per module.

    A (STANDARD)          A-HQ, A-MAD, A-SEV                (plan §3.2)
    B (JOINT_VENTURE)     B-MAD, managed by A-MAD
    C (STANDARD, partner) C-HQ

    hospital     branch B-MAD, procurement A-MAD, ACTIVE   (plan §3.2 "Hospital X")
    sev_works    branch A-SEV, procurement A-SEV, ACTIVE
    sev_for_mad  branch A-SEV, procurement A-MAD, ACTIVE   (A-MAD buys for A-SEV)
    mad_hold     branch A-MAD, procurement A-MAD, ON_HOLD

Each project has its worksite; A-MAD, A-SEV and B-MAD have a warehouse. ``build_people``
adds what only the people need: an inactive company D (with its head office) and an
inactive branch A-OLD.
"""

import re
import time
from dataclasses import dataclass, fields
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Any

from allauth.account.internal.flows.login import AUTHENTICATION_METHODS_SESSION_KEY
from allauth.mfa.models import Authenticator
from django.conf import settings
from django.contrib.auth import BACKEND_SESSION_KEY, HASH_SESSION_KEY, SESSION_KEY
from django.contrib.sessions.backends.db import SessionStore
from django.db.models import F
from django.test import Client
from django.utils import timezone

from apps.access.roles import Role
from apps.accounts.models import User
from apps.accounts.sessions import SESSION_EPOCH_KEY, SESSION_LOGIN_AT_KEY, session_lifetime
from apps.locations.models import Location
from apps.organizations.models import Branch, Company, Membership
from apps.projects.models import Project, ProjectAssignment
from tests.support import PASSWORD, enroll_totp, grant

HOUR = timedelta(hours=1)


# --- organization ----------------------------------------------------------------------


@dataclass
class Org:
    a: Company
    b: Company
    c: Company
    a_hq: Branch
    a_mad: Branch
    a_sev: Branch
    b_mad: Branch
    c_hq: Branch
    hospital: Project
    sev_works: Project
    sev_for_mad: Project
    mad_hold: Project
    warehouses: dict[str, Location]

    def worksite(self, project: Project) -> Location:
        return Location.objects.get(project=project)


def _project(code: str, branch: Branch, procurement: Branch, status: str) -> Project:
    project = Project.objects.create(
        code=code, name=code, branch=branch, procurement_branch=procurement, status=status
    )
    Location.objects.create(
        kind=Location.Kind.WORKSITE, branch=branch, project=project, name=f"site {code}"
    )
    return project


def build_org() -> Org:
    a = Company.objects.create(code="A", legal_name="Company A")
    b = Company.objects.create(code="B", legal_name="UTE B", kind=Company.Kind.JOINT_VENTURE)
    c = Company.objects.create(code="C", legal_name="Company C")
    a_hq = Branch.objects.create(company=a, code="HQ", name="A head office")
    a_mad = Branch.objects.create(company=a, code="MAD", name="A Madrid")
    a_sev = Branch.objects.create(company=a, code="SEV", name="A Sevilla")
    b_mad = Branch.objects.create(company=b, code="MAD", name="B Madrid", managed_by=a_mad)
    c_hq = Branch.objects.create(company=c, code="HQ", name="C head office")
    active, on_hold = Project.Status.ACTIVE, Project.Status.ON_HOLD
    warehouses = {
        branch.name: Location.objects.create(
            kind=Location.Kind.WAREHOUSE, branch=branch, name=f"wh {branch.name}"
        )
        for branch in (a_mad, a_sev, b_mad)
    }
    return Org(
        a=a,
        b=b,
        c=c,
        a_hq=a_hq,
        a_mad=a_mad,
        a_sev=a_sev,
        b_mad=b_mad,
        c_hq=c_hq,
        hospital=_project("HOSP-X", b_mad, a_mad, active),
        sev_works=_project("SEV-1", a_sev, a_sev, active),
        sev_for_mad=_project("SEV-2", a_sev, a_mad, active),
        mad_hold=_project("MAD-1", a_mad, a_mad, on_hold),
        warehouses=warehouses,
    )


def make_user(email: str, **extra: Any) -> User:
    return User.objects.create_user(email, PASSWORD, **extra)


def member(
    role: Role,
    *,
    email: str,
    branch: Branch | None = None,
    company: Company | None = None,
    **window: Any,
) -> User:
    """A user with one membership (``window``: ``valid_from``, ``valid_until``, ``revoked_at``)."""
    user = make_user(email)
    if company is not None:
        User.objects.filter(pk=user.pk).update(employer_company=company)
        user.employer_company = company
    grant(user, role, branch=branch, company=company, **window)
    return user


def assign(
    user: User,
    project: Project,
    kind: str,
    *,
    valid_from: datetime | None = None,
    valid_until: datetime | None = None,
    revoked_at: datetime | None = None,
) -> ProjectAssignment:
    """An assignment written directly (fixtures only), valid from an hour ago by default."""
    return ProjectAssignment.objects.create(
        project=project,
        user=user,
        kind=kind,
        valid_from=valid_from or timezone.now() - HOUR,
        valid_until=valid_until,
        revoked_at=revoked_at,
    )


def also(user: User, role: Role, **kwargs: Any) -> Membership:
    """A second membership for ``user`` (mixed memberships, S6)."""
    return grant(user, role, **kwargs)


# --- people ----------------------------------------------------------------------------


@dataclass
class People:
    """One user per role, plus the mixed, expired, revoked and inactive cases of §8.1.

    Every user has a TOTP authenticator, so any of them can hold a session that passed MFA.
    """

    group_admin: User  # GROUP_ADMIN
    operator: User  # platform operator: superuser and staff, no membership
    mm_mad: User  # MATERIAL_MANAGER of A-MAD: A-MAD and B-MAD
    mm_sev: User  # MATERIAL_MANAGER of A-SEV
    wo_mad: User  # WAREHOUSE_OPERATOR of A-MAD
    tech_mad: User  # TECHNICIAN of A-MAD
    site_manager: User  # SITE_MANAGER (home A-MAD), assigned to the hospital
    partner: User  # PARTNER_REQUESTER of C, assigned to the hospital
    mixed: User  # MATERIAL_MANAGER of A-SEV and TECHNICIAN of A-MAD (S6)
    expired: User  # TECHNICIAN of A-MAD whose membership ended an hour ago
    revoked: User  # MATERIAL_MANAGER of A-MAD whose membership is revoked
    revoked_assignment: User  # SITE_MANAGER whose hospital assignment is revoked
    inactive_partner: User  # PARTNER_REQUESTER of inactive D, assigned to the hospital
    inactive_branch: User  # TECHNICIAN of the inactive branch A-OLD
    deactivated: User  # TECHNICIAN of A-MAD, account deactivated
    nobody: User  # an account without any membership

    def all(self) -> dict[str, User]:
        return {item.name: getattr(self, item.name) for item in fields(self)}


def build_people(org: Org) -> People:
    now = timezone.now()
    d = Company.objects.create(code="D", legal_name="Company D")
    d_hq = Branch.objects.create(company=d, code="HQ", name="D head office")
    a_old = Branch.objects.create(company=org.a, code="OLD", name="A closed branch")
    tm, mm = Role.TECHNICIAN, Role.MATERIAL_MANAGER
    mixed = member(mm, email="mixed@example.org", branch=org.a_sev)
    also(mixed, tm, branch=org.a_mad)
    people = People(
        group_admin=member(Role.GROUP_ADMIN, email="group-admin@example.org"),
        operator=make_user("operator@example.org", is_staff=True, is_superuser=True),
        mm_mad=member(mm, email="mm-mad@example.org", branch=org.a_mad),
        mm_sev=member(mm, email="mm-sev@example.org", branch=org.a_sev),
        wo_mad=member(Role.WAREHOUSE_OPERATOR, email="wo-mad@example.org", branch=org.a_mad),
        tech_mad=member(tm, email="tech-mad@example.org", branch=org.a_mad),
        site_manager=member(Role.SITE_MANAGER, email="site@example.org", branch=org.a_mad),
        partner=member(Role.PARTNER_REQUESTER, email="partner@example.org", company=org.c),
        mixed=mixed,
        expired=member(
            tm,
            email="expired@example.org",
            branch=org.a_mad,
            valid_from=now - 48 * HOUR,
            valid_until=now - HOUR,
        ),
        revoked=member(mm, email="revoked@example.org", branch=org.a_mad, revoked_at=now - HOUR),
        revoked_assignment=member(
            Role.SITE_MANAGER, email="site-revoked@example.org", branch=org.a_mad
        ),
        inactive_partner=member(Role.PARTNER_REQUESTER, email="partner-d@example.org", company=d),
        inactive_branch=member(tm, email="old@example.org", branch=a_old),
        deactivated=member(tm, email="gone@example.org", branch=org.a_mad),
        nobody=make_user("nobody@example.org"),
    )
    manager, partner = ProjectAssignment.Kind.MANAGER, ProjectAssignment.Kind.PARTNER
    assign(people.site_manager, org.hospital, manager)
    assign(people.partner, org.hospital, partner)
    assign(people.revoked_assignment, org.hospital, manager, revoked_at=now - HOUR)
    assign(people.inactive_partner, org.hospital, partner)
    Company.objects.filter(pk=d.pk).update(is_active=False)
    Branch.objects.filter(pk__in=[d_hq.pk, a_old.pk]).update(is_active=False)
    User.objects.filter(pk=people.deactivated.pk).update(is_active=False)
    for user in people.all().values():
        enroll_totp(user)
    return people


def session_for(user: User, *, mfa: bool = True) -> str:
    """A saved session of ``user`` bound to the current epoch; return its key.

    ``mfa``: the session completed MFA now (with the user's own authenticator), as a full
    login would record it. Cheaper than ``Client.force_login`` for many actors.
    """
    epoch, password = User.objects.values_list("session_epoch", "password").get(pk=user.pk)
    user.password = password
    session = SessionStore()
    session[SESSION_KEY] = str(user.pk)
    session[BACKEND_SESSION_KEY] = "allauth.account.auth_backends.AuthenticationBackend"
    session[HASH_SESSION_KEY] = user.get_session_auth_hash()
    session[SESSION_EPOCH_KEY] = epoch
    session[SESSION_LOGIN_AT_KEY] = int(time.time())
    session.set_expiry(session_lifetime(user))
    records: list[dict[str, Any]] = [{"method": "password", "at": time.time()}]
    if mfa:
        authenticator = Authenticator.objects.get(user=user, type=Authenticator.Type.TOTP)
        records.append({"method": "mfa", "at": time.time(), "id": authenticator.pk, "type": "totp"})
    session[AUTHENTICATION_METHODS_SESSION_KEY] = records
    session.create()
    assert session.session_key is not None
    return session.session_key


def change_password_later(user: User) -> None:
    """A password change after the session started: the epoch moves on (S10)."""
    User.objects.filter(pk=user.pk).update(session_epoch=F("session_epoch") + 1)


# --- sentinels -------------------------------------------------------------------------


class Sentinel(StrEnum):
    """Recognisable values whose presence in a response is a leak (plan §8.1, §8.2).

    Each step that adds the model behind a value seeds it into the fixture: supplier name,
    email and phone with the suppliers (M3.2); the price and another project's quantity
    with purchase orders (M5.1). Until then nothing renders them, and the acceptance
    matrix already checks every response against all of them.
    """

    PRICE = "98765.4321"
    SUPPLIER_NAME = "Zyxwvut Sentinel Supplies"
    SUPPLIER_EMAIL = "sentinel-supplier@zyxwvut.example"
    SUPPLIER_PHONE = "+34 699 000 111"
    OTHER_PROJECT_QUANTITY = "4242.125"

    @property
    def pattern(self) -> re.Pattern[str]:
        """How the value may be rendered: localized numbers (``98.765,43``), any case."""
        # ``\s`` also matches the (narrow) no-break spaces some locales group digits with.
        if self in NUMERIC_SENTINELS:
            whole, decimals = self.value.split(".")
            grouped = r"[\s.,']?".join(_thousands(whole))
            return re.compile(rf"(?<!\d){grouped}[.,]{decimals[:2]}")
        if self is Sentinel.SUPPLIER_PHONE:
            national = re.sub(r"\D", "", self.value)[-9:]  # with or without +34
            return re.compile(r"[\s.\-]?".join(national))
        return re.compile(re.escape(self.value), re.IGNORECASE)

    def found_in(self, text: str) -> bool:
        return self.pattern.search(text) is not None


def _thousands(whole: str) -> list[str]:
    head = len(whole) % 3 or 3
    return [whole[:head], *(whole[i : i + 3] for i in range(head, len(whole), 3))]


NUMERIC_SENTINELS = frozenset({Sentinel.PRICE, Sentinel.OTHER_PROJECT_QUANTITY})


def client_for(user: User, *, mfa: bool = True, **defaults: Any) -> Client:
    """A test client signed in as ``user`` (with session MFA unless ``mfa=False``)."""
    client = Client(**defaults)
    client.cookies[settings.SESSION_COOKIE_NAME] = session_for(user, mfa=mfa)
    return client

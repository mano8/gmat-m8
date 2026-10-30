"""The organization of plan §3.2 for the access-engine tests.

Built directly with the ORM (triggers still run), so these tests do not depend on the
services they protect. M2.4 replaces it with the shared fixture of plan §8.1.

    A (STANDARD)          A-HQ, A-MAD, A-SEV
    B (JOINT_VENTURE)     B-MAD, managed by A-MAD
    C (STANDARD, partner) C-HQ

    hospital     branch B-MAD, procurement A-MAD, ACTIVE   (plan §3.2 "Hospital X")
    sev_works    branch A-SEV, procurement A-SEV, ACTIVE
    sev_for_mad  branch A-SEV, procurement A-MAD, ACTIVE   (A-MAD buys for A-SEV)
    mad_hold     branch A-MAD, procurement A-MAD, ON_HOLD

Each project has its worksite; A-MAD, A-SEV and B-MAD have a warehouse.
"""

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

import pytest
from django.utils import timezone

from apps.access.roles import Role
from apps.accounts.models import User
from apps.locations.models import Location
from apps.organizations.models import Branch, Company, Membership
from apps.projects.models import Project, ProjectAssignment
from tests.support import grant

HOUR = timedelta(hours=1)


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


@pytest.fixture
def org(db: None) -> Org:
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


@pytest.fixture
def member(make_user: Callable[..., User]) -> Callable[..., User]:
    """A user with one membership: ``member(Role.X, branch=…, company=…, **validity)``."""
    count = iter(range(1000))

    def make(
        role: Role,
        *,
        branch: Branch | None = None,
        company: Company | None = None,
        email: str | None = None,
        **window: Any,
    ) -> User:
        user = make_user(email or f"{role.lower()}-{next(count)}@example.org")
        if company is not None:
            User.objects.filter(pk=user.pk).update(employer_company=company)
        grant(user, role, branch=branch, company=company, **window)
        return user

    return make


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

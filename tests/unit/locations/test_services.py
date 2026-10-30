"""Location services: warehouses and their rules (plan §3.3, Q3)."""

from collections.abc import Callable

import pytest
from django.core.exceptions import ValidationError

from apps.access.authorization import AccessDenied
from apps.access.generations import org_generation
from apps.access.principals import SessionPrincipal
from apps.access.roles import Role
from apps.accounts.models import User
from apps.audit.models import AuditEvent
from apps.core.idempotency import issue_key
from apps.locations import services
from apps.locations.models import Location
from apps.organizations.models import Branch
from apps.organizations.services import update_branch
from tests.support import grant, principal_for

pytestmark = pytest.mark.django_db


@pytest.fixture
def warehouse(admin_principal: SessionPrincipal, branch: Branch) -> Location:
    return services.create_warehouse(admin_principal, branch_id=branch.pk, name="Almacén MAD")


def test_create_a_warehouse(admin_principal: SessionPrincipal, branch: Branch) -> None:
    key = issue_key(admin_principal.user_id, "location.create_warehouse")
    location = services.create_warehouse(
        admin_principal, branch_id=branch.pk, name="Almacén", address="Calle 1", idempotency_key=key
    )
    assert (location.kind, location.project_id, location.is_active) == (
        Location.Kind.WAREHOUSE,
        None,
        True,
    )
    event = AuditEvent.objects.get(action="location.created")
    assert event.branch_id == branch.pk
    assert str(location) == "Almacén"


def test_not_in_an_inactive_branch(admin_principal: SessionPrincipal, branch: Branch) -> None:
    update_branch(admin_principal, branch.pk, is_active=False)
    with pytest.raises(ValidationError):
        services.create_warehouse(admin_principal, branch_id=branch.pk, name="X")


def test_update_and_deactivate(admin_principal: SessionPrincipal, warehouse: Location) -> None:
    before = org_generation()
    updated = services.update_location(
        admin_principal, warehouse.pk, name="Central", is_active=False
    )
    assert (updated.name, updated.is_active) == ("Central", False)
    assert AuditEvent.objects.get(action="location.updated").data["name"] == {
        "from": "Almacén MAD",
        "to": "Central",
    }
    assert org_generation() == before
    services.update_location(admin_principal, warehouse.pk, name="Central")
    assert AuditEvent.objects.filter(action="location.updated").count() == 1


def test_the_default_warehouse_stays_active(
    admin_principal: SessionPrincipal, branch: Branch, warehouse: Location
) -> None:
    update_branch(admin_principal, branch.pk, default_warehouse_id=warehouse.pk)
    with pytest.raises(ValidationError) as excinfo:
        services.update_location(admin_principal, warehouse.pk, is_active=False)
    assert excinfo.value.error_dict["is_active"][0].code == "default_warehouse"


def test_kind_and_branch_are_fixed(admin_principal: SessionPrincipal, warehouse: Location) -> None:
    with pytest.raises(ValueError, match="branch_id"):
        services.update_location(admin_principal, warehouse.pk, branch_id=warehouse.branch_id)


def test_update_validates(admin_principal: SessionPrincipal, warehouse: Location) -> None:
    with pytest.raises(ValidationError):
        services.update_location(admin_principal, warehouse.pk, name="")


def test_needs_org_manage(
    make_user: Callable[..., User], branch: Branch, warehouse: Location
) -> None:
    operator = make_user("wh@example.org")
    grant(operator, Role.WAREHOUSE_OPERATOR, branch=branch)
    principal = principal_for(operator)
    with pytest.raises(AccessDenied):
        services.create_warehouse(principal, branch_id=branch.pk, name="X")
    with pytest.raises(AccessDenied):
        services.update_location(principal, warehouse.pk, name="X")

"""Management-command base class requiring ``--operator`` (apps.core.commands, plan §3.6)."""

from collections.abc import Callable
from typing import Any

import pytest
from django.core.management import CommandError, call_command
from django.utils import timezone

from apps.access.principals import OperatorPrincipal
from apps.access.roles import Role
from apps.accounts.models import User
from apps.core.commands import OperatorCommand
from apps.organizations.models import Branch
from tests.support import grant

pytestmark = pytest.mark.django_db


class ImportThings(OperatorCommand):
    required_capability = "catalog.manage"
    seen: list[OperatorPrincipal]

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.seen = []

    def handle_operator(self, principal: OperatorPrincipal, *args: Any, **options: Any) -> None:
        self.seen.append(principal)


@pytest.fixture
def manager(make_user: Callable[..., User], branch: Branch) -> User:
    """A material manager: ``catalog.manage`` within the branch."""
    user = make_user("ops@example.org")
    grant(user, Role.MATERIAL_MANAGER, branch=branch)
    return user


def test_operator_is_required() -> None:
    with pytest.raises(CommandError, match="--operator"):
        call_command(ImportThings())


def test_the_operator_runs_as_an_operator_principal(manager: User) -> None:
    command = ImportThings()
    call_command(command, operator="  OPS@example.org ")
    (principal,) = command.seen
    assert principal == OperatorPrincipal(
        user_id=manager.pk,
        email=manager.email,
        command="test_commands",
        capability="catalog.manage",
    )
    assert principal.via == "cli:test_commands"
    assert principal.audit_identity.via == "cli:test_commands"


@pytest.mark.parametrize("state", [{"is_active": False}, {"offboarded_at": timezone.now()}, None])
def test_inactive_offboarded_or_unknown_operators_are_refused(
    make_user: Callable[..., User], state: dict[str, Any] | None
) -> None:
    if state is not None:
        make_user("ops@example.org", **state)
    command = ImportThings()
    with pytest.raises(CommandError, match="active user"):
        call_command(command, operator="ops@example.org")
    assert command.seen == []


def test_an_operator_lacking_the_capability_is_refused(
    make_user: Callable[..., User], branch: Branch
) -> None:
    technician = make_user("ops@example.org")
    grant(technician, Role.TECHNICIAN, branch=branch)
    command = ImportThings()
    with pytest.raises(CommandError, match=r"does not hold catalog\.manage"):
        call_command(command, operator="ops@example.org")
    assert command.seen == []


def test_a_superuser_alone_is_refused(make_user: Callable[..., User]) -> None:
    make_user("ops@example.org", is_staff=True, is_superuser=True)
    with pytest.raises(CommandError, match="does not hold"):
        call_command(ImportThings(), operator="ops@example.org")


def test_a_command_must_declare_its_capability() -> None:
    with pytest.raises(TypeError, match="required_capability"):

        class Undeclared(OperatorCommand):
            pass


def test_handle_operator_is_abstract(manager: User) -> None:
    class Bare(OperatorCommand):
        required_capability = "catalog.manage"

    with pytest.raises(NotImplementedError):
        call_command(Bare(), operator="ops@example.org")


class TouchesTwoDomains(OperatorCommand):
    required_capability = "catalog.manage"
    also_capabilities = ("supplier.manage",)
    seen: dict[str, OperatorPrincipal]

    def handle_operator(self, principal: OperatorPrincipal, *args: Any, **options: Any) -> None:
        self.seen = {
            "main": principal,
            "also": self.principal_for("supplier.manage"),
        }


def test_a_command_can_act_under_several_capabilities_one_principal_each(manager: User) -> None:
    command = TouchesTwoDomains()
    call_command(command, operator=manager.email)
    assert command.seen["main"].capability == "catalog.manage"
    assert command.seen["also"].capability == "supplier.manage"
    assert command.seen["also"].user_id == manager.pk


def test_a_command_with_extra_capabilities_still_needs_the_main_one(
    make_user: Callable[..., User], branch: Branch
) -> None:
    technician = make_user("tech@example.org")
    grant(technician, Role.TECHNICIAN, branch=branch)
    with pytest.raises(CommandError, match=r"does not hold catalog\.manage"):
        call_command(TouchesTwoDomains(), operator=technician.email)


def test_a_shared_base_may_declare_itself_abstract() -> None:
    class SharedBase(OperatorCommand):
        abstract = True

    class Concrete(SharedBase):
        required_capability = "catalog.manage"

    assert Concrete.required_capability == "catalog.manage"
    with pytest.raises(TypeError, match="must declare required_capability"):

        class Missing(SharedBase):
            pass

"""Management-command base class requiring ``--operator`` (apps.core.commands)."""

from collections.abc import Callable
from typing import Any

import pytest
from django.core.management import CommandError, call_command
from django.utils import timezone

from apps.accounts.models import User
from apps.core.commands import Operator, OperatorCommand

pytestmark = pytest.mark.django_db


class ImportThings(OperatorCommand):
    required_capability = "catalog.manage"
    seen: list[Operator]

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.seen = []

    def authorize_operator(self, operator: Operator) -> None:  # M2.3 wires the access engine
        pass

    def handle_operator(self, operator: Operator, *args: Any, **options: Any) -> None:
        self.seen.append(operator)


def test_operator_is_required() -> None:
    with pytest.raises(CommandError, match="--operator"):
        call_command(ImportThings())


def test_the_operator_is_resolved(make_user: Callable[..., User]) -> None:
    user = make_user("ops@example.org")
    command = ImportThings()
    call_command(command, operator="  OPS@example.org ")
    (operator,) = command.seen
    assert operator.user == user
    assert operator.command == "test_commands"
    assert operator.via == "cli:test_commands"


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


def test_until_m2_3_the_base_refuses_to_authorize(make_user: Callable[..., User]) -> None:
    make_user("ops@example.org")

    class Unwired(OperatorCommand):
        required_capability = "catalog.manage"

        def handle_operator(self, operator: Operator, *args: Any, **options: Any) -> None:
            raise AssertionError("must not run")

    with pytest.raises(CommandError, match="not available yet"):
        call_command(Unwired(), operator="ops@example.org")


def test_a_command_must_declare_its_capability() -> None:
    with pytest.raises(TypeError, match="required_capability"):

        class Undeclared(OperatorCommand):
            pass


def test_handle_operator_is_abstract(make_user: Callable[..., User]) -> None:
    class Bare(OperatorCommand):
        required_capability = "catalog.manage"

        def authorize_operator(self, operator: Operator) -> None:
            pass

    make_user("ops@example.org")
    with pytest.raises(NotImplementedError):
        call_command(Bare(), operator="ops@example.org")

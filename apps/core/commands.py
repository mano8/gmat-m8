"""Base class for management commands that act on business data (plan §3.6).

Such a command runs as an ``OperatorPrincipal``: ``--operator <email>`` must name an active,
not offboarded user, and that user's memberships must grant the command's
``required_capability``. Shell access already implies privilege; no session MFA is possible,
and the audit records the identity as the operator with ``via=cli:<command>``.

M2.1 resolves and validates the operator. M2.3 wires ``authorize_operator`` to the access
engine (``OperatorPrincipal`` and the capability check); until then the base refuses to run
any command, so nothing can act with an unchecked operator.
"""

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, ClassVar

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError, CommandParser

if TYPE_CHECKING:
    from apps.accounts.models import User


@dataclass(frozen=True)
class Operator:
    """The validated ``--operator`` of one command run."""

    user: User
    command: str

    @property
    def via(self) -> str:
        return f"cli:{self.command}"


class OperatorCommand(BaseCommand):
    """Requires ``--operator <email>``; subclasses implement ``handle_operator``."""

    required_capability: ClassVar[str]

    def __init_subclass__(cls, **kwargs: Any) -> None:
        super().__init_subclass__(**kwargs)
        if not getattr(cls, "required_capability", ""):
            raise TypeError(f"{cls.__name__} must declare required_capability")

    @property
    def command_name(self) -> str:
        return type(self).__module__.rsplit(".", 1)[-1]

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument(
            "--operator",
            required=True,
            metavar="EMAIL",
            help="Email of the active user on whose authority the command runs",
        )

    def resolve_operator(self, email: str) -> Operator:
        user_model: type[User] = get_user_model()
        user = user_model.objects.filter(email=email.strip().lower()).first()
        if user is None or not user.is_active or user.offboarded_at is not None:
            raise CommandError("--operator must name an active user")
        return Operator(user=user, command=self.command_name)

    def authorize_operator(self, operator: Operator) -> None:
        """Check ``required_capability`` for the operator (M2.3: the access engine)."""
        raise CommandError(
            f"{self.command_name}: operator authorization is not available yet (M2.3)"
        )

    def handle(self, *args: Any, **options: Any) -> str | None:
        operator = self.resolve_operator(options.pop("operator"))
        self.authorize_operator(operator)
        return self.handle_operator(operator, *args, **options)

    def handle_operator(self, operator: Operator, *args: Any, **options: Any) -> str | None:
        raise NotImplementedError

"""Base class for management commands that act on business data (plan §3.6).

Such a command runs as an ``OperatorPrincipal``: ``--operator <email>`` must name an active,
not offboarded user, and that user's memberships must grant the command's
``required_capability``. Shell access already implies privilege; no session MFA is possible,
and the audit records the identity as the operator with ``via=cli:<command>``.

This module is the allowlisted home of ``OperatorPrincipal``. The principal grants the
command's one capability; every service the command calls authorizes it again, in full,
for its own objects.
"""

from typing import TYPE_CHECKING, Any, ClassVar

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError, CommandParser

from apps.access.authorization import authorize_in_txn
from apps.access.exceptions import AccessDenied
from apps.access.principals import OperatorPrincipal
from apps.core.locking import LockOrder, run_in_transaction

if TYPE_CHECKING:
    from apps.accounts.models import User


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

    def resolve_operator(self, email: str) -> User:
        user_model: type[User] = get_user_model()
        user = user_model.objects.filter(email=email.strip().lower()).first()
        if user is None or not user.is_active or user.offboarded_at is not None:
            raise CommandError("--operator must name an active user")
        return user

    def authorize_operator(self, user: User) -> OperatorPrincipal:
        """The operator's principal, once the engine grants ``required_capability``."""
        principal = OperatorPrincipal(
            user_id=user.pk,
            email=user.email,
            command=self.command_name,
            capability=self.required_capability,
        )
        try:
            run_in_transaction(
                lambda: authorize_in_txn(principal, self.required_capability, locks=LockOrder())
            )
        except AccessDenied as exc:
            raise CommandError(
                f"{self.command_name}: the operator does not hold {self.required_capability}"
            ) from exc
        return principal

    def handle(self, *args: Any, **options: Any) -> str | None:
        principal = self.authorize_operator(self.resolve_operator(options.pop("operator")))
        return self.handle_operator(principal, *args, **options)

    def handle_operator(
        self, principal: OperatorPrincipal, *args: Any, **options: Any
    ) -> str | None:
        raise NotImplementedError

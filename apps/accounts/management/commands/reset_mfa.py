"""``manage reset_mfa --operator <email> --email <account> --reason "…"`` (plan §5, M8.3).

For an account that lost its second factors (a lost phone and no recovery code): removes
them all and ends every session of the account, through ``services.reset_user_mfa``. The
operator needs ``org.manage`` (a superuser for a staff account), the audit keeps the reason
with ``via=cli:reset_mfa``, and the account's next sign-in enrolls a new factor where its
roles need one. Check the person's identity before running it (runbook ``user-admin.md``).
"""

from typing import Any

from django.core.exceptions import ValidationError
from django.core.management.base import CommandError, CommandParser

from apps.access.exceptions import AccessDenied
from apps.access.principals import OperatorPrincipal
from apps.access.roles import Capability
from apps.accounts import services
from apps.accounts.models import User
from apps.core.commands import OperatorCommand


class Command(OperatorCommand):
    help = "Remove every second factor of an account and end its sessions."
    required_capability = Capability.ORG_MANAGE

    def add_arguments(self, parser: CommandParser) -> None:
        super().add_arguments(parser)
        parser.add_argument("--email", required=True, help="Address of the account to reset")
        parser.add_argument("--reason", required=True, help="Why (kept in the audit trail)")

    def handle_operator(self, principal: OperatorPrincipal, *args: Any, **options: Any) -> None:
        user = User.objects.filter(email=options["email"].strip().lower()).first()
        if user is None:
            raise CommandError("No account has that address.")
        try:
            services.reset_user_mfa(principal, user.pk, reason=options["reason"])
        except AccessDenied as exc:
            raise CommandError("The operator may not reset this account's factors.") from exc
        except ValidationError as exc:
            raise CommandError(" ".join(exc.messages)) from exc
        self.stdout.write(f"Second factors of {user.email} removed; its sessions are ended.")

"""``manage confirm_account --operator <email> --list | --email <account>`` (plan §9, Q23).

A restore without a preserved database suspends every privileged account. The maintainer
checks each with its owner, including whether its password or factors changed during the
gap (``--credentials-changed``), and confirms it here.
"""

from typing import Any

from django.core.exceptions import ValidationError
from django.core.management.base import CommandError, CommandParser

from apps.access.exceptions import AccessDenied
from apps.access.principals import OperatorPrincipal
from apps.access.roles import Capability
from apps.accounts.models import User
from apps.core.commands import OperatorCommand
from apps.recovery import services


class Command(OperatorCommand):
    help = "List or confirm the accounts a restore suspended."
    required_capability = Capability.ORG_MANAGE

    def add_arguments(self, parser: CommandParser) -> None:
        super().add_arguments(parser)
        what = parser.add_mutually_exclusive_group(required=True)
        what.add_argument("--list", action="store_true", help="The accounts still suspended")
        what.add_argument("--email", help="The account to confirm")
        parser.add_argument(
            "--credentials-changed",
            action="store_true",
            help="Its password or factors changed in the gap: it starts over",
        )

    def handle_operator(self, principal: OperatorPrincipal, *args: Any, **options: Any) -> None:
        if options["list"]:
            for user in services.suspended_accounts():
                self.stdout.write(user.email)
            return
        account = User.objects.filter(email=options["email"].strip().lower()).first()
        if account is None:
            raise CommandError("No account has that address.")
        try:
            services.confirm_suspended_account(
                principal, account.pk, credentials_changed=options["credentials_changed"]
            )
        except AccessDenied as exc:
            raise CommandError("The operator may not confirm this account.") from exc
        except ValidationError as exc:
            raise CommandError(" ".join(exc.messages)) from exc
        self.stdout.write(f"{account.email} can sign in again.")

"""``manage confirm_account --operator <email> --list | --email <account>`` (plan §9, Q23).

A restore without a preserved database suspends every privileged account, and any restore
suspends an account with an email change or a removed second factor after the snapshot. The
maintainer checks each with its owner, including whether its password or factors changed during
the gap (``--credentials-changed``) and whether what was held is genuine (``--email-change``,
``--factor-removal``), and confirms it here.
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
        parser.add_argument(
            "--email-change",
            choices=(services.ACCEPT, services.REJECT),
            default="",
            help="A held email change: accept moves the account, reject keeps its address",
        )
        parser.add_argument(
            "--factor-removal",
            choices=(services.ACCEPT, services.REJECT),
            default="",
            help="A held removal of second factors: accept removes them, reject keeps them",
        )

    def handle_operator(self, principal: OperatorPrincipal, *args: Any, **options: Any) -> None:
        if options["list"]:
            self._list()
            return
        self._confirm(principal, options)

    def _list(self) -> None:
        for user in services.suspended_accounts():
            self.stdout.write(self._line(user))

    def _line(self, user: User) -> str:
        parts = [user.email]
        target = services.held_email_change(user)
        if target:
            parts.append(f"-> {target} (email change)")
        factors = services.held_factor_removal(user)
        if factors:
            parts.append(f"({', '.join(factors)} removal)")
        return " ".join(parts)

    def _confirm(self, principal: OperatorPrincipal, options: dict[str, Any]) -> None:
        account = User.objects.filter(email=options["email"].strip().lower()).first()
        if account is None:
            raise CommandError("No account has that address.")
        try:
            confirmed = services.confirm_suspended_account(
                principal,
                account.pk,
                credentials_changed=options["credentials_changed"],
                email_change=options["email_change"],
                factor_removal=options["factor_removal"],
            )
        except AccessDenied as exc:
            raise CommandError("The operator may not confirm this account.") from exc
        except ValidationError as exc:
            raise CommandError(" ".join(exc.messages)) from exc
        self.stdout.write(f"{confirmed.email} can sign in again.")

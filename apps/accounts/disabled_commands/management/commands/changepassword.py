"""Django's ``changepassword``, disabled (ADR-0030).

It sets a password with no audit event and no session epoch bump, so the account's other
sessions would live on and a restore could not replay the change (ADR-0016). A person sets a
new password through *Forgot password* (allauth: rate-limited, the owner told by email); an
account whose address is lost is offboarded and invited again (docs/runbooks/user-admin.md).
"""

from typing import Any

from django.core.management.base import BaseCommand, CommandError, CommandParser


class Command(BaseCommand):
    help = "Disabled: use 'Forgot password' on the sign-in page (docs/runbooks/user-admin.md)."

    def add_arguments(self, parser: CommandParser) -> None:
        # Django's own arguments, accepted so that every call reaches the refusal.
        parser.add_argument("username", nargs="?")
        parser.add_argument("--database")

    def handle(self, *args: Any, **options: Any) -> None:
        raise CommandError(
            "changepassword is disabled: it writes no audit event a restore could replay. "
            "The person uses 'Forgot password' on the sign-in page; if the address is lost, "
            "offboard the account and send a new invitation (docs/runbooks/user-admin.md)."
        )

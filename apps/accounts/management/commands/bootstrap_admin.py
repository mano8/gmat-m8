"""``manage bootstrap_admin --email … --company-code … [--superuser]`` (plan §5, F76, Q29).

Invites the first group administrator from a shell on the server, only while no active
``GROUP_ADMIN`` exists (and, with ``--superuser``, no active superuser). In one audited
transaction it creates the company and its head office if they are missing and a
``NEW_ACCOUNT`` invitation granting ``GROUP_ADMIN``; the one-time setup link is printed to
this terminal only. Accepting it sets the password and verifies the address; MFA is enrolled
at the first sign-in, as for any administrator. Running it again before the link is used
replaces the link.
"""

from typing import Any

from django.core.exceptions import ValidationError
from django.core.management.base import BaseCommand, CommandError, CommandParser

from apps.access.exceptions import AccessDenied
from apps.accounts import services, tokens


class Command(BaseCommand):
    help = "Invite the first group administrator and print the one-time setup link."

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument("--email", required=True, help="Address of the first administrator")
        parser.add_argument(
            "--company-code", required=True, help="Code of the first company (created if missing)"
        )
        parser.add_argument("--company-name", default="", help="Legal name of a new company")
        parser.add_argument(
            "--superuser",
            action="store_true",
            help="Also make the account a platform operator (staff and superuser)",
        )

    def handle(self, *args: Any, **options: Any) -> None:
        email = str(options["email"]).strip().lower()
        principal = tokens.bootstrap_principal(email, platform_operator=options["superuser"])
        try:
            result = services.bootstrap_admin(
                principal,
                company_code=str(options["company_code"]).strip().upper(),
                company_name=str(options["company_name"]).strip(),
                platform_operator=options["superuser"],
            )
        except AccessDenied as exc:
            raise CommandError(
                "bootstrap_admin refused: an active group administrator already exists"
                + (" or an active superuser" if options["superuser"] else "")
                + "; invite people from the app instead."
            ) from exc
        except ValidationError as exc:
            raise CommandError("; ".join(exc.messages)) from exc
        if result.company_created:
            self.stdout.write(f"Company {result.invitation.employer_company} created.")
        hours = int(tokens.INVITATION_LIFETIME.total_seconds() // 3600)
        self.stdout.write(
            f"Open this link within {hours} hours to set the password of {email}."
            " It works once and is shown only here:"
        )
        self.stdout.write(result.link)

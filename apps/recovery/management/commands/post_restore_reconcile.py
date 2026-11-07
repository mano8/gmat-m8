"""``entrypoint.sh post-restore-reconcile --operator <email> …`` (plan §9, ``recover.sh``).

Runs once on a restored database, before either gate opens. ``--failed-state -`` reads what
``ops/failed-state.sql`` saved from the failed database from standard input;
``--no-failed-state`` says that database is gone (a lost VPS, Q23). One of them is required,
so nobody suspends every privileged account or skips the replay by leaving a flag out.

Prints the reconciliation report as JSON. It names accounts: keep it with the incident.
"""

import json
import sys
from pathlib import Path
from typing import Any

from django.core.exceptions import ValidationError
from django.core.management.base import CommandError, CommandParser

from apps.access.exceptions import AccessDenied
from apps.access.principals import OperatorPrincipal
from apps.access.roles import Capability
from apps.core.commands import OperatorCommand
from apps.recovery import failed_state, services

STDIN = "-"


def _read(source: str) -> str:
    return sys.stdin.read() if source == STDIN else Path(source).read_text(encoding="utf-8")


class Command(OperatorCommand):
    help = "Stop a restored database from resurrecting access or re-sending mail."
    required_capability = Capability.ORG_MANAGE

    def add_arguments(self, parser: CommandParser) -> None:
        super().add_arguments(parser)
        state = parser.add_mutually_exclusive_group(required=True)
        state.add_argument(
            "--failed-state", metavar="FILE", help="The saved failed state (`-`: standard input)"
        )
        state.add_argument(
            "--no-failed-state",
            action="store_true",
            help="The failed database is lost: suspend every privileged account (Q23)",
        )
        parser.add_argument(
            "--gap-offboarded",
            action="append",
            default=[],
            metavar="EMAIL",
            help="Without a failed state: an account offboarded in the gap",
        )
        parser.add_argument(
            "--gap-credentials-changed",
            action="append",
            default=[],
            metavar="EMAIL",
            help="Without a failed state: an account whose credentials changed in the gap",
        )

    def _state(self, options: dict[str, Any]) -> failed_state.FailedState | None:
        if options["no_failed_state"]:
            return None
        if options["gap_offboarded"] or options["gap_credentials_changed"]:
            raise CommandError("A failed state says what happened: leave out the --gap options.")
        try:
            return failed_state.parse(_read(options["failed_state"]))
        except (OSError, failed_state.InvalidFailedState) as exc:
            raise CommandError(str(exc)) from exc

    def handle_operator(self, principal: OperatorPrincipal, *args: Any, **options: Any) -> None:
        state = self._state(options)
        gap = None
        if state is None:
            gap = services.Gap(
                offboarded=frozenset(options["gap_offboarded"]),
                credentials_changed=frozenset(options["gap_credentials_changed"]),
            )
        try:
            report = services.post_restore_reconcile(principal, failed_state=state, gap=gap)
        except AccessDenied as exc:
            raise CommandError("The operator may not reconcile a restore.") from exc
        except ValidationError as exc:
            raise CommandError(" ".join(exc.messages)) from exc
        self.stdout.write(json.dumps(report.as_dict(), indent=2, sort_keys=True))

"""``manage outbox_quarantine --operator <email> <action>`` (plan §9, step 4; Q25).

After a restore nothing held back is sent until a human says so:

- ``list``: what is held, per kind, and each supplier dispatch;
- ``release-notifications`` / ``discard-notifications``: business notifications, in bulk;
- ``confirm-dispatch ID`` / ``release-dispatch ID`` / ``discard-dispatch ID``: one supplier
  dispatch, after checking with the supplier: it has it, send it again, or never send it.
"""

from collections.abc import Callable
from typing import Any
from uuid import UUID

from django.core.exceptions import ObjectDoesNotExist, ValidationError
from django.core.management.base import CommandError, CommandParser

from apps.access.exceptions import AccessDenied
from apps.access.principals import OperatorPrincipal
from apps.access.roles import Capability
from apps.core.commands import OperatorCommand
from apps.notifications import selectors
from apps.notifications import services as outbox
from apps.purchasing import services as purchasing

DISPATCH_ACTIONS: dict[str, Callable[..., Any]] = {
    "confirm-dispatch": purchasing.confirm_dispatch,
    "release-dispatch": purchasing.resend_dispatch,
    "discard-dispatch": purchasing.discard_dispatch,
}


class Command(OperatorCommand):
    help = "List, release or discard the outbox messages a restore held back."
    required_capability = Capability.ORG_MANAGE
    also_capabilities = (Capability.PO_MANAGE,)

    def add_arguments(self, parser: CommandParser) -> None:
        super().add_arguments(parser)
        parser.add_argument(
            "action",
            choices=["list", "release-notifications", "discard-notifications", *DISPATCH_ACTIONS],
        )
        parser.add_argument("dispatch", nargs="?", type=UUID, help="The dispatch's id")
        parser.add_argument("--note", default="", help="Kept in the audit trail of the order")

    def _list(self) -> None:
        for kind, count in selectors.quarantined_counts().items():
            self.stdout.write(f"{kind}: {count}")
        for row in selectors.quarantined_dispatches():
            self.stdout.write(
                f"{row.pk} {row.kind} order {row.source_po_id} ({row.source_ref}, "
                f"{row.attempts} attempts)"
            )

    def _dispatch(self, action: str, dispatch: UUID | None, note: str) -> None:
        if dispatch is None:
            raise CommandError(f"{action} needs the dispatch's id.")
        principal = self.principal_for(Capability.PO_MANAGE)
        message = DISPATCH_ACTIONS[action](principal, dispatch, note=note)
        self.stdout.write(f"{dispatch}: {message.status}")

    def _act(self, principal: OperatorPrincipal, options: dict[str, Any]) -> None:
        action = options["action"]
        if action == "list":
            self._list()
        elif action == "release-notifications":
            released = outbox.release_quarantined_notifications(principal)
            self.stdout.write(f"{released} notifications queued again.")
        elif action == "discard-notifications":
            discarded = outbox.discard_quarantined_notifications(principal)
            self.stdout.write(f"{discarded} notifications discarded.")
        else:
            self._dispatch(action, options["dispatch"], options["note"])

    def handle_operator(self, principal: OperatorPrincipal, *args: Any, **options: Any) -> None:
        try:
            self._act(principal, options)
        except AccessDenied as exc:
            raise CommandError("The operator may not do this.") from exc
        except ObjectDoesNotExist as exc:
            raise CommandError("No such dispatch.") from exc
        except ValidationError as exc:
            raise CommandError(" ".join(exc.messages)) from exc

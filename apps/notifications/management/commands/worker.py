"""``manage.py worker``: the outbox sender and housekeeping (plan §5, ``entrypoint.sh worker``).

Not an ``OperatorCommand``: it acts for nobody but the fixed ``system:worker`` principal,
which ``apps.notifications.worker`` constructs.
"""

from typing import Any

from django.core.management.base import BaseCommand

from apps.notifications.worker import main


class Command(BaseCommand):
    help = "Send queued email and run the hourly housekeeping until stopped (SIGTERM)."

    def handle(self, *args: Any, **options: Any) -> None:
        main()

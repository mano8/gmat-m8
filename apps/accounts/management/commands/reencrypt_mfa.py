"""``manage reencrypt_mfa --operator <email>`` (M8.5): the last step of rotating the MFA key.

Moves every stored TOTP secret and recovery-code seed to the first of ``MFA_ENCRYPTION_KEYS``
(``services.reencrypt_mfa_secrets``), so that an older key can leave the list. Every key that
still opens something must be listed while it runs. Audited with ``via=cli:reencrypt_mfa``. The
steps around it are in the runbook (``user-admin.md``).
"""

from typing import Any

from django.core.management.base import CommandError

from apps.access.principals import OperatorPrincipal
from apps.access.roles import Capability
from apps.accounts import services
from apps.core.commands import OperatorCommand


class Command(OperatorCommand):
    help = "Re-encrypt every stored second-factor secret under the newest MFA key."
    required_capability = Capability.ORG_MANAGE

    def handle_operator(self, principal: OperatorPrincipal, *args: Any, **options: Any) -> None:
        done = services.reencrypt_mfa_secrets(principal)
        self.stdout.write(f"{done.moved} moved to the newest key, {done.already} already there.")
        if done.unreadable:
            listed = ", ".join(str(user_id) for user_id in done.unreadable)
            raise CommandError(
                f"No listed key opens the second factors of these accounts: {listed}. "
                "Find the key, or reset them (manage reset_mfa). Keep every old key until then."
            )

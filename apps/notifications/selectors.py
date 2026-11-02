"""Reads of the notification screens (M7.1)."""

from apps.access.context import AccessContext
from apps.notifications.models import NotificationPreference


def email_opt_out(ctx: AccessContext) -> bool:
    """Whether the signed-in user turned business email off."""
    if ctx.user_id is None:
        return False
    return NotificationPreference.objects.filter(user_id=ctx.user_id, email_opt_out=True).exists()

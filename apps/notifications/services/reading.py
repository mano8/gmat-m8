"""A person reading their own in-app notifications (M17.3, Q99).

``open_notification`` marks one read and says where it leads; ``mark_all_read`` marks every one
the person may still see. Both act only on the principal's own rows, through ``in_app.visible``:
another person's notification, or one whose request left the person's scope, is not found (404),
never revealed. Any current member may use them (``catalog.view``, held by every role; session
MFA for a privileged role, as everywhere). Each is idempotent by nature (reading twice changes
nothing), so they take no idempotency key, and neither is audited: a person reading their own
notifications changes nobody's access or data.
"""

from uuid import UUID

from django.db.models.functions import Now
from django.urls import reverse

from apps.access.authorization import authorize_in_txn
from apps.access.context import AccessContext
from apps.access.exceptions import NotInScope
from apps.access.principals import SessionPrincipal
from apps.access.roles import Capability
from apps.core.locking import LockOrder, transactional
from apps.notifications import in_app
from apps.notifications.models import InAppNotification


def _own(principal: SessionPrincipal) -> AccessContext:
    """The principal's access, checked (``authorize_in_txn``), to read their own rows with."""
    user = authorize_in_txn(principal, Capability.CATALOG_VIEW, locks=LockOrder())
    return AccessContext.for_user(user)


@transactional
def open_notification(principal: SessionPrincipal, notification_id: UUID) -> str:
    """Mark the principal's notification ``notification_id`` read (if it is not already) and
    return the path of the request page it leads to."""
    rows = in_app.visible(_own(principal)).filter(pk=notification_id)
    found: InAppNotification | None = rows.first()
    if found is None:
        raise NotInScope(Capability.CATALOG_VIEW)
    rows.filter(read_at__isnull=True).update(read_at=Now(), updated_at=Now())
    return reverse(found.spec.page, args=[found.object_id])


@transactional
def mark_all_read(principal: SessionPrincipal) -> int:
    """Mark every unread notification the principal may still see read; return how many."""
    unread = in_app.visible(_own(principal)).filter(read_at__isnull=True)
    return unread.update(read_at=Now(), updated_at=Now())

"""The outbox's services: the worker's transactions and the settings of business email.

- ``claims``: ``claim_next``, ``record_outcome``, ``reconcile_abandoned``, ``run_housekeeping`` and
  the release or discard of what a restore held back (plan §5, §9), all for the worker's
  ``SystemPrincipal`` or an operator;
- ``own``: ``set_own_channels`` and ``set_own_notices``, the signed-in person's own channels and
  choices (M17.2), and ``set_email_opt_out``, the email case of the first (M7.1);
- ``preferences``: ``set_notice_default`` and ``set_person_notices``, a group administrator's
  settings per channel (M15.2, M17.2);
- ``reading``: ``open_notification`` and ``mark_all_read``, the signed-in person reading their own
  in-app notifications (M17.3).
"""

from apps.notifications.services.claims import (
    ABANDONED_ERROR,
    OUTBOX_SEND,
    RETRY_BASE_DELAY,
    RETRY_MAX_DELAY,
    RETRY_POLICY,
    Claim,
    Reconciled,
    RetryPolicy,
    Settled,
    abandon_after,
    claim_next,
    discard_quarantined_notifications,
    reconcile_abandoned,
    record_outcome,
    release_quarantined_notifications,
    retry_delay,
    run_housekeeping,
)
from apps.notifications.services.own import (
    OWN_CHANNELS_OPERATION,
    OWN_NOTICES_OPERATION,
    set_email_opt_out,
    set_own_channels,
    set_own_notices,
)
from apps.notifications.services.preferences import (
    NOTICE_DEFAULT_OPERATION,
    PERSON_NOTICES_OPERATION,
    set_notice_default,
    set_person_notices,
)
from apps.notifications.services.reading import mark_all_read, open_notification

__all__ = [
    "ABANDONED_ERROR",
    "NOTICE_DEFAULT_OPERATION",
    "OUTBOX_SEND",
    "OWN_CHANNELS_OPERATION",
    "OWN_NOTICES_OPERATION",
    "PERSON_NOTICES_OPERATION",
    "RETRY_BASE_DELAY",
    "RETRY_MAX_DELAY",
    "RETRY_POLICY",
    "Claim",
    "Reconciled",
    "RetryPolicy",
    "Settled",
    "abandon_after",
    "claim_next",
    "discard_quarantined_notifications",
    "mark_all_read",
    "open_notification",
    "reconcile_abandoned",
    "record_outcome",
    "release_quarantined_notifications",
    "retry_delay",
    "run_housekeeping",
    "set_email_opt_out",
    "set_notice_default",
    "set_own_channels",
    "set_own_notices",
    "set_person_notices",
]

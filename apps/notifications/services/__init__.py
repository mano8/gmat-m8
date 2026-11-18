"""The outbox's services: the worker's transactions and the settings of business email.

- ``claims``: ``claim_next``, ``record_outcome``, ``reconcile_abandoned``, ``run_housekeeping`` and
  the release or discard of what a restore held back (plan §5, §9), all for the worker's
  ``SystemPrincipal`` or an operator;
- ``preferences``: ``set_email_opt_out`` (a person's own switch, M7.1), ``set_notice_default`` and
  ``set_person_notices`` (a group administrator's settings, M15.2).
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
from apps.notifications.services.preferences import (
    NOTICE_DEFAULT_OPERATION,
    PERSON_NOTICES_OPERATION,
    set_email_opt_out,
    set_notice_default,
    set_person_notices,
)

__all__ = [
    "ABANDONED_ERROR",
    "NOTICE_DEFAULT_OPERATION",
    "OUTBOX_SEND",
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
    "reconcile_abandoned",
    "record_outcome",
    "release_quarantined_notifications",
    "retry_delay",
    "run_housekeeping",
    "set_email_opt_out",
    "set_notice_default",
    "set_person_notices",
]

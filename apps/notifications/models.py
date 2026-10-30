"""The email outbox (plan §4.2, §5 "Email: outbox and worker").

Every email is a row of ``EmailOutbox``, written in the transaction that causes it; only the
``worker`` process sends, after committing an ``EmailAttempt`` for the message. The content
(subject, bodies, recipients, eligibility) is frozen at insert: a trigger
(``notifications.0002``) refuses any change to it, except wiping a body to the empty string.
"""

from django.contrib.postgres.fields import ArrayField
from django.db import models
from django.db.models import Q
from django.db.models.functions import Now
from django.utils.translation import gettext_lazy as _

from apps.core.models import BaseModel

# Fan-out (plan §5): one message per event, at most this many BCC recipients each.
MAX_RECIPIENTS = 20


class Kind(models.TextChoices):
    NOTIFICATION = "NOTIFICATION", _("Notification")
    SECURITY = "SECURITY", _("Security email")
    PO_ORDER = "PO_ORDER", _("Purchase order")
    PO_CANCELLATION = "PO_CANCELLATION", _("Purchase-order cancellation")


# Supplier dispatches: tied to a purchase order, locked after it (L4 → L4b, plan §4.7).
DISPATCH_KINDS = frozenset({Kind.PO_ORDER, Kind.PO_CANCELLATION})


class Status(models.TextChoices):
    QUEUED = "QUEUED", _("Queued")
    SENDING = "SENDING", _("Sending")
    SENT = "SENT", _("Sent")
    FAILED = "FAILED", _("Failed")
    AMBIGUOUS = "AMBIGUOUS", _("Ambiguous")
    SUPERSEDED = "SUPERSEDED", _("Superseded")
    CANCELLED = "CANCELLED", _("Cancelled")
    SUPPRESSED = "SUPPRESSED", _("Suppressed")
    EXPIRED = "EXPIRED", _("Expired")
    QUARANTINED = "QUARANTINED", _("Quarantined")


class Outcome(models.TextChoices):
    """How one attempt ended (plan §5, step 3)."""

    SENT = "SENT", _("Sent")
    # A definite rejection before DATA, or the connection was refused: nothing was delivered.
    FAILED = "FAILED", _("Failed")
    # A timeout or disconnect after DATA started: the message may have been delivered.
    AMBIGUOUS = "AMBIGUOUS", _("Ambiguous")


class EmailOutbox(BaseModel):
    """One email, frozen at insert and re-checked when the worker claims it.

    Recipients and eligibility per kind:

    - ``NOTIFICATION``: ``recipient_users`` (1-20 user ids); ``eligibility_rule`` is a
      capability that each recipient must still hold on the referenced object (or anywhere,
      without an object) at the database clock of the claim;
    - ``SECURITY``: ``recipient_address``; ``eligibility_rule`` names a rule of
      ``apps.notifications.eligibility`` on the referenced target (``account.active``). The
      bodies are sealed with ``OUTBOX_ENCRYPTION_KEYS``, wiped once sent and expired after
      ``expires_at``;
    - ``PO_ORDER`` / ``PO_CANCELLATION``: ``recipient_address`` and ``source_po``; their
      eligibility belongs to the purchasing app (``apps.notifications.dispatch``).

    ``source_po`` is a plain UUID until the purchase-order model exists (M5.1 turns it into
    a foreign key on the same column).
    """

    kind = models.CharField(max_length=20, choices=Kind.choices)
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.QUEUED)
    sensitive = models.BooleanField(default=False)
    recipient_users = ArrayField(models.UUIDField(), default=list, blank=True)
    recipient_address = models.EmailField(max_length=254, blank=True)
    eligibility_rule = models.CharField(max_length=100, blank=True)
    eligibility_object_type = models.CharField(max_length=100, blank=True)
    eligibility_object_id = models.UUIDField(null=True, blank=True)
    source_po = models.UUIDField(null=True, blank=True)
    source_ref = models.CharField(max_length=100, blank=True)
    from_email = models.CharField(max_length=254)
    subject = models.TextField()
    # Sealed (``apps.notifications.crypto``) when ``sensitive``; the empty string once wiped.
    body = models.TextField(blank=True)
    html_body = models.TextField(blank=True)
    not_before = models.DateTimeField(db_default=Now())
    expires_at = models.DateTimeField(null=True, blank=True)
    attempts = models.PositiveIntegerField(default=0)
    # Outcomes are recorded only while this still points at the attempt reporting them.
    current_attempt = models.ForeignKey(
        "EmailAttempt",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="+",
    )
    message_id = models.CharField(max_length=255, blank=True)
    last_error = models.CharField(max_length=255, blank=True)
    sent_at = models.DateTimeField(null=True, blank=True)
    wiped_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        constraints = [
            models.CheckConstraint(
                condition=(
                    Q(
                        kind=Kind.NOTIFICATION,
                        recipient_users__len__gte=1,
                        recipient_users__len__lte=MAX_RECIPIENTS,
                        recipient_address="",
                        source_po__isnull=True,
                    )
                    & ~Q(eligibility_rule="")
                )
                | (
                    Q(
                        kind=Kind.SECURITY,
                        recipient_users__len=0,
                        source_po__isnull=True,
                        expires_at__isnull=False,
                        eligibility_object_id__isnull=False,
                    )
                    & ~Q(recipient_address="")
                    & ~Q(eligibility_rule="")
                    & ~Q(eligibility_object_type="")
                )
                | (
                    Q(
                        kind__in=[Kind.PO_ORDER, Kind.PO_CANCELLATION],
                        recipient_users__len=0,
                        source_po__isnull=False,
                    )
                    & ~Q(recipient_address="")
                ),
                name="notifications_outbox_shape_per_kind",
            ),
            # Security emails, and only they, carry sensitive bodies (plan §5).
            models.CheckConstraint(
                condition=Q(sensitive=True, kind=Kind.SECURITY)
                | (Q(sensitive=False) & ~Q(kind=Kind.SECURITY)),
                name="notifications_outbox_sensitive_iff_security",
            ),
            models.CheckConstraint(
                condition=~Q(status=Status.SENDING) | Q(current_attempt__isnull=False),
                name="notifications_outbox_sending_has_attempt",
            ),
            models.CheckConstraint(
                condition=Q(eligibility_object_id__isnull=True, eligibility_object_type="")
                | (Q(eligibility_object_id__isnull=False) & ~Q(eligibility_object_type="")),
                name="notifications_outbox_eligibility_object_whole",
            ),
            # One cancellation notice per order: the obligation queues it once (F53).
            models.UniqueConstraint(
                fields=["source_po"],
                condition=Q(kind=Kind.PO_CANCELLATION),
                name="notifications_outbox_one_cancellation_per_po",
            ),
        ]
        indexes = [
            models.Index(
                fields=["not_before", "id"],
                condition=Q(status=Status.QUEUED),
                name="notifications_outbox_due",
            ),
            models.Index(
                fields=["status"],
                condition=Q(status=Status.SENDING),
                name="notifications_outbox_sending",
            ),
            models.Index(
                fields=["expires_at"],
                condition=Q(sensitive=True, wiped_at__isnull=True),
                name="notifications_outbox_unwiped",
            ),
            models.Index(fields=["source_po"], name="notifications_outbox_source_po"),
        ]

    def __str__(self) -> str:
        return f"{self.kind} {self.id} ({self.status})"

    @property
    def is_dispatch(self) -> bool:
        return self.kind in DISPATCH_KINDS


class EmailAttempt(BaseModel):
    """One claim of a message, committed before any network I/O (plan §5, step 1).

    ``started_at`` is the database clock of the claim; the abandoned-attempt reconciliation
    compares it with the database clock too. ``recipient_users`` are the notification
    recipients that passed the eligibility check of this claim (ids, never addresses).
    ``abandoned`` marks an outcome set by that reconciliation; the worker's own late outcome
    may still replace it.
    """

    message = models.ForeignKey(EmailOutbox, on_delete=models.PROTECT, related_name="attempt_set")
    number = models.PositiveIntegerField()
    started_at = models.DateTimeField(db_default=Now())
    finished_at = models.DateTimeField(null=True, blank=True)
    outcome = models.CharField(max_length=20, choices=Outcome.choices, blank=True)
    abandoned = models.BooleanField(default=False)
    recipient_users = ArrayField(models.UUIDField(), default=list, blank=True)
    error = models.CharField(max_length=255, blank=True)
    worker = models.CharField(max_length=100, blank=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["message", "number"], name="notifications_attempt_number"
            ),
            # Target of the composite key that keeps ``current_attempt`` on its own message.
            models.UniqueConstraint(fields=["id", "message"], name="notifications_attempt_id_msg"),
            models.CheckConstraint(
                condition=Q(number__gte=1), name="notifications_attempt_number_gte_1"
            ),
            models.CheckConstraint(
                condition=Q(outcome="", finished_at__isnull=True)
                | (~Q(outcome="") & Q(finished_at__isnull=False)),
                name="notifications_attempt_outcome_finished",
            ),
        ]

    def __str__(self) -> str:
        return f"attempt {self.number} of {self.message_id}"

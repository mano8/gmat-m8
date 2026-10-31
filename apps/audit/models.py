"""The audit trail. Rows are written by ``apps.audit.recorder.record`` and never change."""

from django.conf import settings
from django.core.serializers.json import DjangoJSONEncoder
from django.db import models
from django.db.models.functions import Now

from apps.core.ids import new_id


class AuditEvent(models.Model):
    """One audited action, written in the same transaction as the change it records.

    Append-only: a trigger (``audit.0002``) refuses ``UPDATE`` and ``DELETE``, and the
    runtime database role has no ``TRUNCATE`` privilege. ``occurred_at`` is the database
    clock. ``branch_id`` has no foreign key on purpose: history must not take locks on, or
    depend on, the live organization rows.
    """

    id = models.UUIDField(primary_key=True, default=new_id, editable=False)
    occurred_at = models.DateTimeField(db_default=Now(), editable=False)
    actor = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="+",
    )
    # The audit identity (plan §3.6): the user's email or a fixed name such as
    # ``system:worker``, and how the action arrived (``cli:<command>``, ``invitation:<id>``…).
    actor_label = models.CharField(max_length=254)
    via = models.CharField(max_length=100, blank=True)
    action = models.CharField(max_length=100)
    object_type = models.CharField(max_length=100, blank=True)
    object_id = models.UUIDField(null=True, blank=True)
    branch_id = models.UUIDField(null=True, blank=True)
    data = models.JSONField(default=dict, blank=True, encoder=DjangoJSONEncoder)
    request_id = models.CharField(max_length=64, blank=True)
    ip = models.GenericIPAddressField(null=True, blank=True)

    class Meta:
        ordering = ["-occurred_at", "-id"]
        indexes = [
            models.Index(fields=["occurred_at"], name="audit_occurred_at"),
            models.Index(fields=["object_type", "object_id"], name="audit_object"),
            models.Index(fields=["actor", "occurred_at"], name="audit_actor"),
            models.Index(fields=["branch_id", "occurred_at"], name="audit_branch"),
        ]

    def __str__(self) -> str:
        return f"{self.action} by {self.actor_label}"

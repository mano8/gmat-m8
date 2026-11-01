"""Attachments of documents: photos and PDFs of requests, receipts and handovers (plan §5, Q8).

Models hold data and constraints only; every change goes through ``services`` (and the
worker's ``housekeeping``).

- ``Attachment``: one stored file of exactly one parent. There is one nullable foreign key
  per parent type and a CHECK "exactly one set", never a generic relation. A trigger
  (``attachments.0002``) keeps the row immutable except for its removal (a soft delete,
  once), so the parent link can never be changed (no relinking), and lets a row be deleted
  only once removed (the purge).
- ``StorageQuota``: the singleton that admission and finalization lock at L8: the limit,
  the bytes stored (removed files included until they are purged) and the bytes reserved by
  uploads in progress.
- ``AttachmentReservation``: one upload's slot and bytes, from admission to finalization
  (``PROCESSING`` → ``FINALIZED``) or release (``RELEASED``), with a lease the worker
  enforces.
- ``MediaDeletion``: a file whose row the purge removed; the file itself is deleted after a
  grace period longer than the longest backup (plan §9, two-phase purge).

Files live in ``ATTACHMENT_ROOT`` under a random ``storage_key``; nothing serves that
folder directly (``views.download``).
"""

from typing import Any
from uuid import UUID

from django.conf import settings
from django.db import models
from django.db.models import Q
from django.utils.translation import gettext_lazy as _

from apps.core.models import BaseModel
from apps.handovers.models import Handover
from apps.material_requests.models import MaterialRequest
from apps.receiving.models import GoodsReceipt

QUOTA_ID = UUID(int=2)

# For the CHECK constraints (``Meta`` cannot see the nested choices).
CONTENT_TYPES = ["image/webp", "application/pdf"]
STATES = ["PROCESSING", "FINALIZED", "RELEASED"]

PARENT_FIELDS = ("request", "receipt", "handover")


def exactly_one_parent() -> Q:
    """The CHECK of both models: one parent foreign key set, the others null."""
    result = Q(pk__in=[])
    for chosen in PARENT_FIELDS:
        condition = Q(**{f"{chosen}__isnull": False})
        for other in PARENT_FIELDS:
            if other != chosen:
                condition &= Q(**{f"{other}__isnull": True})
        result |= condition
    return result


def _parent(model: type[models.Model], label: Any, related_name: str) -> models.ForeignKey:  # type: ignore[type-arg]
    return models.ForeignKey(
        model,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name=related_name,
        verbose_name=label,
    )


class Attachment(BaseModel):
    """A stored file of one request, receipt or handover."""

    class ContentType(models.TextChoices):
        WEBP = "image/webp", _("Image")
        PDF = "application/pdf", _("PDF")

    request = _parent(MaterialRequest, _("request"), "attachments")
    receipt = _parent(GoodsReceipt, _("receipt"), "attachments")
    handover = _parent(Handover, _("handover"), "attachments")
    storage_key = models.CharField(_("storage key"), max_length=64, unique=True, editable=False)
    file_name = models.CharField(_("file name"), max_length=150)
    content_type = models.CharField(_("type"), max_length=30, choices=ContentType)
    size_bytes = models.PositiveBigIntegerField(_("size"))
    sha256 = models.CharField(_("SHA-256"), max_length=64)
    uploaded_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="+",
        verbose_name=_("uploaded by"),
    )
    uploaded_at = models.DateTimeField(_("uploaded at"))
    removed_at = models.DateTimeField(_("removed at"), null=True, blank=True)
    removed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="+",
        verbose_name=_("removed by"),
    )
    removal_reason = models.TextField(_("removal reason"), blank=True)

    class Meta:
        verbose_name = _("attachment")
        verbose_name_plural = _("attachments")
        ordering = ["uploaded_at", "id"]
        constraints = [
            models.CheckConstraint(condition=exactly_one_parent(), name="att_one_parent"),
            models.CheckConstraint(
                condition=Q(content_type__in=CONTENT_TYPES), name="att_content_type_valid"
            ),
            models.CheckConstraint(condition=Q(size_bytes__gt=0), name="att_size_positive"),
            models.CheckConstraint(
                condition=Q(removed_at__isnull=True, removed_by__isnull=True, removal_reason="")
                | Q(removed_at__isnull=False, removed_by__isnull=False),
                name="att_removal_fields",
            ),
        ]
        indexes = [
            models.Index(fields=["removed_at"], name="att_removed"),
        ]

    def __str__(self) -> str:
        return self.file_name

    @property
    def is_image(self) -> bool:
        return self.content_type == self.ContentType.WEBP


class StorageQuota(models.Model):
    """The installation's attachment storage (Q22): one row, locked at L8."""

    id = models.UUIDField(primary_key=True, default=QUOTA_ID, editable=False)
    limit_bytes = models.PositiveBigIntegerField(_("limit"))
    used_bytes = models.PositiveBigIntegerField(_("stored"), default=0)
    reserved_bytes = models.PositiveBigIntegerField(_("reserved"), default=0)
    alert_raised_at = models.DateTimeField(_("alert raised at"), null=True, blank=True)

    class Meta:
        verbose_name = _("storage quota")
        verbose_name_plural = _("storage quota")
        constraints = [
            models.CheckConstraint(condition=Q(id=QUOTA_ID), name="att_quota_singleton"),
        ]

    def __str__(self) -> str:
        return f"{self.used_bytes} + {self.reserved_bytes} / {self.limit_bytes}"


class AttachmentReservation(BaseModel):
    """One upload's slot and bytes, held from admission until finalization or release."""

    class State(models.TextChoices):
        PROCESSING = "PROCESSING", _("Processing")
        FINALIZED = "FINALIZED", _("Finalized")
        RELEASED = "RELEASED", _("Released")

    request = _parent(MaterialRequest, _("request"), "+")
    receipt = _parent(GoodsReceipt, _("receipt"), "+")
    handover = _parent(Handover, _("handover"), "+")
    reserved_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="+",
        verbose_name=_("reserved by"),
    )
    reserved_bytes = models.PositiveBigIntegerField(_("reserved"))
    state = models.CharField(_("state"), max_length=10, choices=State, default=State.PROCESSING)
    lease_until = models.DateTimeField(_("lease until"))
    finished_at = models.DateTimeField(_("finished at"), null=True, blank=True)

    class Meta:
        verbose_name = _("attachment reservation")
        verbose_name_plural = _("attachment reservations")
        ordering = ["-created_at", "-id"]
        constraints = [
            models.CheckConstraint(condition=exactly_one_parent(), name="att_rsv_one_parent"),
            models.CheckConstraint(condition=Q(state__in=STATES), name="att_rsv_state_valid"),
            models.CheckConstraint(
                condition=Q(reserved_bytes__gt=0), name="att_rsv_bytes_positive"
            ),
            models.CheckConstraint(
                condition=Q(state="PROCESSING", finished_at__isnull=True)
                | (~Q(state="PROCESSING") & Q(finished_at__isnull=False)),
                name="att_rsv_finished_when_done",
            ),
        ]
        indexes = [
            models.Index(
                fields=["lease_until"],
                condition=Q(state="PROCESSING"),
                name="att_rsv_processing",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.state} {self.reserved_bytes}"


class MediaDeletion(BaseModel):
    """A purged attachment's file, deleted from disk once ``delete_after`` has passed."""

    storage_key = models.CharField(_("storage key"), max_length=64, unique=True)
    size_bytes = models.PositiveBigIntegerField(_("size"))
    delete_after = models.DateTimeField(_("delete after"))

    class Meta:
        verbose_name = _("media deletion")
        verbose_name_plural = _("media deletions")
        ordering = ["delete_after", "id"]

    def __str__(self) -> str:
        return self.storage_key

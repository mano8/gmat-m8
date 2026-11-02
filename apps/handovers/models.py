"""Handovers of material to a project's people (plan §4.2, §4.5, §4.6).

Two kinds:

- ``DIRECT_RECEIPT``: goods received at a worksite are handed over on the spot. Such a
  handover is created and voided only together with its goods receipt (``direct``, I8):
  each line carries exactly one receipt attribution, for the same request line and
  quantity (C4), and an attribution has at most one handover line (unique), so a site
  receipt can never be counted twice;
- ``FROM_WAREHOUSE``: material taken from a warehouse for a project (``services``).

D of a request line is the sum of its lines on posted handovers of both kinds
(``ledger``). Models hold data and constraints only. A handover is **immutable once
posted** (I11): only its void fields change, once; a database trigger (``handovers.0002``)
refuses anything else and any update or delete of a line. C3: ``(request_line_id,
project_id)`` and ``(handover_id, project_id)`` keep every line on the handover's project.
"""

from django.conf import settings
from django.db import models
from django.db.models import Q
from django.utils.translation import gettext_lazy as _

from apps.access.scopes import Scope, ScopedQuerySet
from apps.core.fields import QuantityField
from apps.core.models import BaseModel
from apps.locations.models import Location
from apps.material_requests.models import MaterialRequestLine
from apps.organizations.models import Branch
from apps.projects.models import Project
from apps.receiving.models import GoodsReceipt, ReceiptAttribution

# For the CHECK constraints (``Meta`` cannot see the nested choices).
KINDS = ["FROM_WAREHOUSE", "DIRECT_RECEIPT"]
STATUSES = ["POSTED", "VOIDED"]
VOID_FIELDS = Q(voided_at__isnull=False, voided_by__isnull=False) & ~Q(void_reason="")
NO_VOID_FIELDS = Q(voided_at__isnull=True, voided_by__isnull=True, void_reason="")


def handover_q(scope: Scope, prefix: str = "") -> Q:
    """Handovers by their branch (S5) and, for assigned projects, by their project."""
    return scope.document_q(branch=f"{prefix}branch", project=f"{prefix}project")


class HandoverQuerySet(ScopedQuerySet["Handover"]):
    def scope_q(self, scope: Scope) -> Q:
        return handover_q(scope)


class HandoverLineQuerySet(ScopedQuerySet["HandoverLine"]):
    def scope_q(self, scope: Scope) -> Q:
        return handover_q(scope, "handover__")


class Handover(BaseModel):
    """Material handed over to a project, from a warehouse or straight from a receipt."""

    class Kind(models.TextChoices):
        FROM_WAREHOUSE = "FROM_WAREHOUSE", _("From a warehouse")
        DIRECT_RECEIPT = "DIRECT_RECEIPT", _("Received on site")

    class Status(models.TextChoices):
        POSTED = "POSTED", _("Posted")
        VOIDED = "VOIDED", _("Voided")

    number = models.CharField(_("number"), max_length=64, unique=True, editable=False)
    kind = models.CharField(_("kind"), max_length=15, choices=Kind)
    branch = models.ForeignKey(
        Branch, on_delete=models.PROTECT, related_name="handovers", verbose_name=_("branch")
    )
    source_location = models.ForeignKey(
        Location, on_delete=models.PROTECT, related_name="handovers", verbose_name=_("from")
    )
    project = models.ForeignKey(
        Project, on_delete=models.PROTECT, related_name="handovers", verbose_name=_("project")
    )
    handed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="+",
        verbose_name=_("handed over by"),
    )
    received_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="+",
        verbose_name=_("received by"),
    )
    received_by_name = models.CharField(_("received by (name)"), max_length=200, blank=True)
    handed_at = models.DateTimeField(_("handed over at"))
    goods_receipt = models.OneToOneField(
        GoodsReceipt,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="direct_handover",
        verbose_name=_("goods receipt"),
    )
    status = models.CharField(_("status"), max_length=10, choices=Status, default=Status.POSTED)
    voided_at = models.DateTimeField(_("voided at"), null=True, blank=True)
    voided_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="+",
        verbose_name=_("voided by"),
    )
    void_reason = models.TextField(_("void reason"), blank=True)

    objects = HandoverQuerySet.as_manager()

    class Meta:
        verbose_name = _("handover")
        verbose_name_plural = _("handovers")
        ordering = ["-handed_at", "-id"]
        constraints = [
            models.CheckConstraint(condition=Q(kind__in=KINDS), name="ho_kind_valid"),
            models.CheckConstraint(condition=Q(status__in=STATUSES), name="ho_status_valid"),
            # kind = DIRECT_RECEIPT if and only if goods_receipt is set (§4.2).
            models.CheckConstraint(
                condition=Q(kind="DIRECT_RECEIPT", goods_receipt__isnull=False)
                | Q(kind="FROM_WAREHOUSE", goods_receipt__isnull=True),
                name="ho_direct_has_receipt",
            ),
            models.CheckConstraint(
                condition=Q(received_by__isnull=False) | ~Q(received_by_name=""),
                name="ho_receiver_named",
            ),
            models.CheckConstraint(
                condition=(Q(status="POSTED") & NO_VOID_FIELDS)
                | (Q(status="VOIDED") & VOID_FIELDS),
                name="ho_void_fields",
            ),
            # The target of the lines' (handover_id, project_id) foreign key (C3).
            models.UniqueConstraint(fields=["id", "project"], name="ho_id_project"),
        ]
        indexes = [
            models.Index(fields=["branch", "status"], name="ho_branch"),
            models.Index(fields=["project", "status"], name="ho_project"),
        ]

    def __str__(self) -> str:
        return self.number


class HandoverLine(BaseModel):
    """A quantity of one request line handed over; a direct one names its attribution."""

    handover = models.ForeignKey(
        Handover, on_delete=models.PROTECT, related_name="lines", verbose_name=_("handover")
    )
    # Denormalized from the handover so C3 can be a composite foreign key (§4.6).
    project = models.ForeignKey(
        Project, on_delete=models.PROTECT, related_name="+", verbose_name=_("project")
    )
    request_line = models.ForeignKey(
        MaterialRequestLine,
        on_delete=models.PROTECT,
        related_name="handover_lines",
        verbose_name=_("request line"),
    )
    qty = QuantityField(_("quantity"))
    # Required on a DIRECT_RECEIPT handover, absent otherwise (``direct``, I8).
    receipt_attribution = models.OneToOneField(
        ReceiptAttribution,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="handover_line",
        verbose_name=_("receipt attribution"),
    )

    objects = HandoverLineQuerySet.as_manager()

    class Meta:
        verbose_name = _("handover line")
        verbose_name_plural = _("handover lines")
        ordering = ["handover", "id"]
        constraints = [
            models.CheckConstraint(condition=Q(qty__gt=0), name="ho_line_qty_positive"),
        ]

    def __str__(self) -> str:
        return f"{self.handover} · {self.request_line}"

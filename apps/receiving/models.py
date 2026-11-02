"""Goods receipts, their lines and the attributions to allocations (plan §4.2, §4.5, §4.6).

Models hold data and constraints only; every change goes through ``services``. A receipt is
**immutable once posted** (I11): only its void fields change, once, and a database trigger
(``receiving.0002``) refuses anything else, as it refuses any update or delete of a line or
an attribution. Corrections are a void and a new receipt.

- ``GoodsReceipt``: a document of its order's ``branch`` (S5); one at a worksite also
  belongs to that project, which is how its site manager reaches it (receipt.view ○ site).
  ``location`` is where the goods really arrived, which may differ from the order's
  ship-to (the difference is audited).
- ``GoodsReceiptLine``: what arrived of one order line: accepted (``qty_received``, which
  counts as received), rejected, the incident and the part nobody could take
  (``surplus_qty``). C1: ``(receipt_id, po_id)`` and ``(po_line_id, po_id)`` keep the line
  on its receipt's order.
- ``ReceiptAttribution``: the part of an accepted quantity attributed to one allocation;
  R of a request line is the sum of its attributions on posted receipts, over every order
  (F69). C2: ``(allocation_id, po_line_id)`` and ``(receipt_line_id, po_line_id)`` keep the
  allocation on the received order line. Per receipt line, Σ attributions + surplus =
  accepted (I7, ``services``).
"""

from django.conf import settings
from django.db import models
from django.db.models import Q
from django.utils.translation import gettext_lazy as _

from apps.access.scopes import Scope, ScopedQuerySet
from apps.core.fields import QUANTITY_ZERO, QuantityField
from apps.core.models import BaseModel
from apps.locations.models import Location
from apps.organizations.models import Branch
from apps.purchasing.models import PurchaseAllocation, PurchaseOrder, PurchaseOrderLine

# For the CHECK constraints (``Meta`` cannot see the nested choices).
STATUSES = ["POSTED", "VOIDED"]
ISSUES = ["NONE", "SHORT", "WRONG_ITEM", "DAMAGED", "NOTE_MISMATCH"]
VOID_FIELDS = Q(voided_at__isnull=False, voided_by__isnull=False) & ~Q(void_reason="")
NO_VOID_FIELDS = Q(voided_at__isnull=True, voided_by__isnull=True, void_reason="")


def receipt_q(scope: Scope, prefix: str = "") -> Q:
    """Receipts by their branch (S5) and, for assigned projects, by the worksite's project."""
    return scope.document_q(branch=f"{prefix}branch", project=f"{prefix}location__project")


class ReceiptQuerySet(ScopedQuerySet["GoodsReceipt"]):
    def scope_q(self, scope: Scope) -> Q:
        return receipt_q(scope)


class ReceiptLineQuerySet(ScopedQuerySet["GoodsReceiptLine"]):
    def scope_q(self, scope: Scope) -> Q:
        return receipt_q(scope, "receipt__")


class AttributionQuerySet(ScopedQuerySet["ReceiptAttribution"]):
    def scope_q(self, scope: Scope) -> Q:
        return receipt_q(scope, "receipt_line__receipt__")


class GoodsReceipt(BaseModel):
    """Goods of one purchase order that arrived at one location (plan §4.2)."""

    class Status(models.TextChoices):
        POSTED = "POSTED", _("Posted")
        VOIDED = "VOIDED", _("Voided")

    number = models.CharField(_("number"), max_length=64, unique=True, editable=False)
    po = models.ForeignKey(
        PurchaseOrder, on_delete=models.PROTECT, related_name="receipts", verbose_name=_("order")
    )
    branch = models.ForeignKey(
        Branch, on_delete=models.PROTECT, related_name="goods_receipts", verbose_name=_("branch")
    )
    location = models.ForeignKey(
        Location,
        on_delete=models.PROTECT,
        related_name="goods_receipts",
        verbose_name=_("location"),
    )
    received_at = models.DateTimeField(_("received at"))
    received_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="+",
        verbose_name=_("received by"),
    )
    delivery_note_ref = models.CharField(_("delivery note"), max_length=100, blank=True)
    notes = models.TextField(_("notes"), blank=True)
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

    objects = ReceiptQuerySet.as_manager()

    class Meta:
        verbose_name = _("goods receipt")
        verbose_name_plural = _("goods receipts")
        ordering = ["-received_at", "-id"]
        constraints = [
            models.CheckConstraint(condition=Q(status__in=STATUSES), name="rcpt_status_valid"),
            models.CheckConstraint(
                condition=(Q(status="POSTED") & NO_VOID_FIELDS)
                | (Q(status="VOIDED") & VOID_FIELDS),
                name="rcpt_void_fields",
            ),
            # The target of the receipt lines' (receipt_id, po_id) foreign key (C1).
            models.UniqueConstraint(fields=["id", "po"], name="rcpt_id_order"),
        ]
        indexes = [
            models.Index(fields=["po", "status"], name="rcpt_order"),
            models.Index(fields=["branch", "status"], name="rcpt_branch"),
        ]

    def __str__(self) -> str:
        return self.number


class GoodsReceiptLine(BaseModel):
    """What arrived of one order line: accepted, rejected, the incident, the surplus."""

    class Issue(models.TextChoices):
        NONE = "NONE", _("No incident")
        SHORT = "SHORT", _("Short delivery")
        WRONG_ITEM = "WRONG_ITEM", _("Wrong item")
        DAMAGED = "DAMAGED", _("Damaged")
        NOTE_MISMATCH = "NOTE_MISMATCH", _("Delivery note mismatch")

    receipt = models.ForeignKey(
        GoodsReceipt, on_delete=models.PROTECT, related_name="lines", verbose_name=_("receipt")
    )
    # Denormalized from the receipt so C1 can be a composite foreign key (§4.6).
    po = models.ForeignKey(
        PurchaseOrder, on_delete=models.PROTECT, related_name="+", verbose_name=_("order")
    )
    po_line = models.ForeignKey(
        PurchaseOrderLine,
        on_delete=models.PROTECT,
        related_name="receipt_lines",
        verbose_name=_("order line"),
    )
    qty_received = QuantityField(_("accepted quantity"))
    qty_rejected = QuantityField(_("rejected quantity"), default=QUANTITY_ZERO)
    issue = models.CharField(_("incident"), max_length=15, choices=Issue, default=Issue.NONE)
    issue_note = models.TextField(_("incident note"), blank=True)
    surplus_qty = QuantityField(_("surplus"), default=QUANTITY_ZERO)

    objects = ReceiptLineQuerySet.as_manager()

    class Meta:
        verbose_name = _("goods receipt line")
        verbose_name_plural = _("goods receipt lines")
        ordering = ["receipt", "po_line__line_no"]
        constraints = [
            models.UniqueConstraint(fields=["receipt", "po_line"], name="rcpt_line_one_per_line"),
            # The target of the attributions' (receipt_line_id, po_line_id) key (C2).
            models.UniqueConstraint(fields=["id", "po_line"], name="rcpt_line_id_po_line"),
            models.CheckConstraint(condition=Q(issue__in=ISSUES), name="rcpt_line_issue_valid"),
            models.CheckConstraint(
                condition=Q(qty_received__gte=0, qty_rejected__gte=0, surplus_qty__gte=0),
                name="rcpt_line_quantities_not_negative",
            ),
            models.CheckConstraint(
                condition=Q(qty_received__gt=0) | Q(qty_rejected__gt=0),
                name="rcpt_line_not_empty",
            ),
            models.CheckConstraint(
                condition=Q(surplus_qty__lte=models.F("qty_received")),
                name="rcpt_line_surplus_within_received",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.receipt} · {self.po_line}"


class ReceiptAttribution(BaseModel):
    """The part of a receipt line's accepted quantity that serves one allocation (§4.5)."""

    receipt_line = models.ForeignKey(
        GoodsReceiptLine,
        on_delete=models.PROTECT,
        related_name="attributions",
        verbose_name=_("receipt line"),
    )
    allocation = models.ForeignKey(
        PurchaseAllocation,
        on_delete=models.PROTECT,
        related_name="attributions",
        verbose_name=_("allocation"),
    )
    # Denormalized from the receipt line so C2 can be a composite foreign key (§4.6).
    po_line = models.ForeignKey(
        PurchaseOrderLine, on_delete=models.PROTECT, related_name="+", verbose_name=_("order line")
    )
    qty = QuantityField(_("quantity"))

    objects = AttributionQuerySet.as_manager()

    class Meta:
        verbose_name = _("receipt attribution")
        verbose_name_plural = _("receipt attributions")
        ordering = ["receipt_line", "id"]
        constraints = [
            models.UniqueConstraint(
                fields=["receipt_line", "allocation"], name="rcpt_attribution_pair"
            ),
            models.CheckConstraint(condition=Q(qty__gt=0), name="rcpt_attribution_qty_positive"),
        ]

    def __str__(self) -> str:
        return f"{self.receipt_line} → {self.allocation}"

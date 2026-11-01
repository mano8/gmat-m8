"""Purchase orders, their lines and the allocations to request lines (plan §4.2, §4.3, §4.5).

Models hold data and constraints only; every change goes through ``services/``, whose
state changes are checked against ``transitions.ALLOWED_TRANSITIONS``. Received, pending,
draft-reserved and the other ledger quantities are derived (``waterfall``, ``ledger``),
never stored. Prices are optional (Q10) and reach only ``price.view`` holders.

- ``PurchaseOrder``: a document of its ``branch`` (the procurement branch, S5); the buyer
  is that branch's company (Q2). ``number``, the snapshots and ``issued_*`` are set at
  issue; documents, exports and dispatches use the snapshots, never the live master data.
- ``PurchaseOrderLine``: one material per open line of an order (quantities are
  aggregated per material). C9 binds ``(material_id, unit_id)`` to the material
  (``purchasing.0002``); ``UNIQUE (id, po_id)`` is the target of the receipt lines' C1 key.
- ``PurchaseAllocation``: ``qty`` is the commitment ceiling a request line holds on an order
  line. It grows only through an authorized allocation (C5) and shrinks only through
  audited trims, never below what is attributed (§4.5, F57). ``UNIQUE (id, po_line_id)``
  is the target of the attributions' C2 key (M6.1).
"""

from django.conf import settings
from django.core.serializers.json import DjangoJSONEncoder
from django.db import models
from django.db.models import Q
from django.utils.translation import gettext_lazy as _

from apps.access.scopes import Scope, ScopedQuerySet
from apps.catalog.models import Material, UnitOfMeasure
from apps.core.fields import PriceField, QuantityField
from apps.core.models import BaseModel
from apps.locations.models import Location
from apps.material_requests.models import MaterialRequestLine
from apps.organizations.models import Branch, Company
from apps.suppliers.models import Supplier

# For the CHECK constraints (``Meta`` cannot see the nested choices).
STATUSES = ["DRAFT", "ISSUED", "CLOSED", "CANCELLED"]
ISSUED_FIELDS = Q(number__isnull=False, issued_at__isnull=False, issued_by__isnull=False)
NOT_ISSUED_FIELDS = Q(number__isnull=True, issued_at__isnull=True, issued_by__isnull=True)


class OrderQuerySet(ScopedQuerySet["PurchaseOrder"]):
    def scope_q(self, scope: Scope) -> Q:
        """Orders by their branch (S5)."""
        return scope.document_q(branch="branch")


class OrderLineQuerySet(ScopedQuerySet["PurchaseOrderLine"]):
    def scope_q(self, scope: Scope) -> Q:
        return scope.document_q(branch="po__branch")


class AllocationQuerySet(ScopedQuerySet["PurchaseAllocation"]):
    def scope_q(self, scope: Scope) -> Q:
        return scope.document_q(branch="po_line__po__branch")


class PurchaseOrder(BaseModel):
    """An order to one supplier, shipped to one location (plan §4.2).

    ``revision`` is 0 when issued and grows by one with each amendment. Receipt progress
    (not received, partial, received) is derived.
    """

    class Status(models.TextChoices):
        DRAFT = "DRAFT", _("Draft")
        ISSUED = "ISSUED", _("Issued")
        CLOSED = "CLOSED", _("Closed")
        CANCELLED = "CANCELLED", _("Cancelled")

    # NULL until issued: unique once set.
    number = models.CharField(
        _("number"), max_length=64, null=True, blank=True, unique=True, editable=False
    )
    revision = models.PositiveIntegerField(_("revision"), default=0)
    branch = models.ForeignKey(
        Branch, on_delete=models.PROTECT, related_name="purchase_orders", verbose_name=_("branch")
    )
    buyer_company = models.ForeignKey(
        Company,
        on_delete=models.PROTECT,
        related_name="purchase_orders",
        verbose_name=_("buyer"),
    )
    supplier = models.ForeignKey(
        Supplier,
        on_delete=models.PROTECT,
        related_name="purchase_orders",
        verbose_name=_("supplier"),
    )
    ship_to = models.ForeignKey(
        Location,
        on_delete=models.PROTECT,
        related_name="purchase_orders",
        verbose_name=_("ship to"),
    )
    status = models.CharField(_("status"), max_length=10, choices=Status, default=Status.DRAFT)
    expected_date = models.DateField(_("expected date"), null=True, blank=True)
    supplier_ref = models.CharField(_("supplier reference"), max_length=100, blank=True)
    notes = models.TextField(_("notes"), blank=True)
    currency = models.CharField(_("currency"), max_length=3, default="EUR", editable=False)
    issued_at = models.DateTimeField(_("issued at"), null=True, blank=True)
    issued_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="+",
        verbose_name=_("issued by"),
    )
    # A dispatch was in flight when the order was cancelled: the supplier is owed a
    # cancellation notice once it ends sent or ambiguous (plan §5, F53; M5.3).
    cancellation_notice_required = models.BooleanField(
        _("cancellation notice required"), default=False
    )
    buyer_snapshot = models.JSONField(
        _("buyer (as issued)"), default=dict, blank=True, encoder=DjangoJSONEncoder
    )
    supplier_snapshot = models.JSONField(
        _("supplier (as issued)"), default=dict, blank=True, encoder=DjangoJSONEncoder
    )
    ship_to_snapshot = models.JSONField(
        _("ship to (as issued)"), default=dict, blank=True, encoder=DjangoJSONEncoder
    )

    objects = OrderQuerySet.as_manager()

    class Meta:
        verbose_name = _("purchase order")
        verbose_name_plural = _("purchase orders")
        ordering = ["-created_at"]
        constraints = [
            models.CheckConstraint(condition=Q(status__in=STATUSES), name="po_order_status_valid"),
            # Drafts have no number; issued and closed orders do; a cancelled order is
            # either a discarded draft or a cancelled issued order.
            models.CheckConstraint(
                condition=(Q(status="DRAFT") & NOT_ISSUED_FIELDS)
                | (Q(status__in=["ISSUED", "CLOSED"]) & ISSUED_FIELDS)
                | (Q(status="CANCELLED") & (NOT_ISSUED_FIELDS | ISSUED_FIELDS)),
                name="po_order_issue_fields",
            ),
            models.CheckConstraint(
                condition=Q(number__isnull=False) | Q(revision=0), name="po_order_revision"
            ),
        ]
        indexes = [
            models.Index(fields=["branch", "status"], name="po_order_branch"),
            models.Index(fields=["supplier", "status"], name="po_order_supplier"),
        ]

    def __str__(self) -> str:
        return self.number or str(_("Draft order"))


class PurchaseOrderLine(BaseModel):
    """One material on an order; ``unit`` is the material's (C9)."""

    po = models.ForeignKey(
        PurchaseOrder, on_delete=models.PROTECT, related_name="lines", verbose_name=_("order")
    )
    line_no = models.PositiveSmallIntegerField(_("line"))
    material = models.ForeignKey(
        Material,
        on_delete=models.PROTECT,
        related_name="purchase_lines",
        verbose_name=_("material"),
    )
    unit = models.ForeignKey(
        UnitOfMeasure, on_delete=models.PROTECT, related_name="+", verbose_name=_("unit")
    )
    ordered_qty = QuantityField(_("ordered quantity"))
    unit_price = PriceField(_("unit price"), null=True, blank=True)
    discount_pct = models.DecimalField(
        _("discount (%)"), max_digits=5, decimal_places=2, null=True, blank=True
    )
    material_snapshot = models.JSONField(
        _("material (as issued)"), default=dict, blank=True, encoder=DjangoJSONEncoder
    )
    closed_short_at = models.DateTimeField(_("closed short at"), null=True, blank=True)
    close_reason = models.TextField(_("close reason"), blank=True)

    objects = OrderLineQuerySet.as_manager()

    class Meta:
        verbose_name = _("purchase order line")
        verbose_name_plural = _("purchase order lines")
        ordering = ["po", "line_no"]
        constraints = [
            models.UniqueConstraint(fields=["po", "line_no"], name="po_line_number"),
            # The target of the receipt lines' (po_line_id, po_id) foreign key (C1, M6.1).
            models.UniqueConstraint(fields=["id", "po"], name="po_line_id_order"),
            # Quantities are aggregated per material: one open line each.
            models.UniqueConstraint(
                fields=["po", "material"],
                condition=Q(closed_short_at__isnull=True),
                name="po_line_one_open_per_material",
            ),
            models.CheckConstraint(condition=Q(line_no__gte=1), name="po_line_no_positive"),
            models.CheckConstraint(condition=Q(ordered_qty__gt=0), name="po_line_ordered_positive"),
            models.CheckConstraint(
                condition=Q(unit_price__isnull=True) | Q(unit_price__gte=0),
                name="po_line_price_not_negative",
            ),
            models.CheckConstraint(
                condition=Q(discount_pct__isnull=True)
                | Q(discount_pct__gte=0, discount_pct__lte=100),
                name="po_line_discount_range",
            ),
            models.CheckConstraint(
                condition=Q(closed_short_at__isnull=True, close_reason="")
                | (Q(closed_short_at__isnull=False) & ~Q(close_reason="")),
                name="po_line_closure_fields",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.po} #{self.line_no}"


class PurchaseAllocation(BaseModel):
    """The part of an order line that serves one request line (plan §4.5).

    On a draft order it reserves demand (Q); on an issued one it is the ceiling of the
    line's pending waterfall (P). Trimmed to zero, never deleted, once an order is issued.
    """

    po_line = models.ForeignKey(
        PurchaseOrderLine,
        on_delete=models.PROTECT,
        related_name="allocations",
        verbose_name=_("order line"),
    )
    request_line = models.ForeignKey(
        MaterialRequestLine,
        on_delete=models.PROTECT,
        related_name="allocations",
        verbose_name=_("request line"),
    )
    qty = QuantityField(_("allocated quantity"))

    objects = AllocationQuerySet.as_manager()

    class Meta:
        verbose_name = _("purchase allocation")
        verbose_name_plural = _("purchase allocations")
        ordering = ["po_line", "id"]
        constraints = [
            models.UniqueConstraint(fields=["po_line", "request_line"], name="po_allocation_pair"),
            # The target of the attributions' (allocation_id, po_line_id) key (C2, M6.1).
            models.UniqueConstraint(fields=["id", "po_line"], name="po_allocation_id_line"),
            models.CheckConstraint(condition=Q(qty__gte=0), name="po_allocation_qty_not_negative"),
        ]

    def __str__(self) -> str:
        return f"{self.po_line} → {self.request_line}"

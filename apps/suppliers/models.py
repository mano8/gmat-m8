"""Suppliers (plan §4.2): master data shared by the whole installation.

Only ``supplier.view`` holders read a supplier (group administrators, material managers
and warehouse operators); everyone else gets, at most, the display name of the purchase
orders they receive goods for (plan §3.4). Suppliers are deactivated, never deleted.
"""

from django.db import models
from django.db.models import Q
from django.db.models.functions import Lower
from django.utils.translation import gettext_lazy as _

from apps.access.scopes import InstallationQuerySet
from apps.core.models import BaseModel


class SupplierQuerySet(InstallationQuerySet["Supplier"]):
    pass


class Supplier(BaseModel):
    """``name`` is unique ignoring case; ``tax_id`` is unique when given."""

    name = models.CharField(_("name"), max_length=200)
    # NULL, not "", when absent: the unique index only covers suppliers that have one.
    tax_id = models.CharField(_("tax id"), max_length=32, null=True, blank=True)  # noqa: DJ001
    contact_name = models.CharField(_("contact person"), max_length=200, blank=True)
    email = models.EmailField(_("email"), blank=True)
    phone = models.CharField(_("phone"), max_length=40, blank=True)
    address = models.TextField(_("address"), blank=True)
    notes = models.TextField(_("notes"), blank=True)
    is_active = models.BooleanField(_("active"), default=True)

    objects = SupplierQuerySet.as_manager()

    class Meta:
        verbose_name = _("supplier")
        verbose_name_plural = _("suppliers")
        ordering = ["name"]
        constraints = [
            models.UniqueConstraint(Lower("name"), name="suppliers_supplier_name"),
            models.UniqueConstraint(
                fields=["tax_id"],
                condition=Q(tax_id__isnull=False),
                name="suppliers_supplier_tax_id",
            ),
            models.CheckConstraint(condition=~Q(name=""), name="suppliers_supplier_name_set"),
            models.CheckConstraint(
                condition=~Q(tax_id=""), name="suppliers_supplier_tax_id_not_empty"
            ),
        ]

    def __str__(self) -> str:
        return self.name

    def clean(self) -> None:
        super().clean()
        self.name = self.name.strip()
        self.tax_id = (self.tax_id or "").strip() or None

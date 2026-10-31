"""Material catalog: categories, units of measure and materials (plan §4.2).

Master data shared by the whole installation: a ``catalog.view`` / ``catalog.manage`` grant
reaches all of it (``InstallationQuerySet``). Models hold data and constraints only; every
change goes through ``services.py``. Materials are deactivated, never deleted.

Search runs on ``catalog_fold(text)``, an immutable ``lower(unaccent(text))`` created by
``catalog.0001`` with trigram indexes over it (``fold()`` below builds the same expression).
"""

from django.core.validators import MaxValueValidator, RegexValidator
from django.db import models
from django.db.models import F, Func, Q, TextField
from django.db.models.functions import Lower
from django.utils.translation import gettext_lazy as _

from apps.access.scopes import InstallationQuerySet
from apps.core.models import BaseModel

EAN_PATTERN = r"^[0-9]{8,14}$"  # EAN-8, UPC-A, EAN-13 and GTIN-14
REFERENCE_PATTERN = r"^[^\x00-\x1f\x7f]+$"  # one line of printable text
reference_validator = RegexValidator(
    REFERENCE_PATTERN, _("A reference is one line of text without control characters.")
)
ean_validator = RegexValidator(EAN_PATTERN, _("Use 8 to 14 digits."))
MAX_UNIT_DECIMALS = 3  # a quantity has three decimal places (plan §4.2)


class Fold(Func):
    """``catalog_fold(expression)``: lower-case and accent-free, as the trigram indexes."""

    function = "catalog_fold"
    output_field = TextField()


def fold(expression: str | F | models.Value) -> Fold:
    return Fold(F(expression) if isinstance(expression, str) else expression)


class CategoryQuerySet(InstallationQuerySet["MaterialCategory"]):
    pass


class UnitQuerySet(InstallationQuerySet["UnitOfMeasure"]):
    pass


class MaterialQuerySet(InstallationQuerySet["Material"]):
    pass


class MaterialCategory(BaseModel):
    """A catalog category, at most two levels (a category with children is a root).

    Enforced in the database by the ``catalog_category_two_levels`` trigger.
    """

    name = models.CharField(_("name"), max_length=100)
    parent = models.ForeignKey(
        "self",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="children",
        verbose_name=_("parent category"),
    )

    objects = CategoryQuerySet.as_manager()

    class Meta:
        verbose_name = _("material category")
        verbose_name_plural = _("material categories")
        ordering = ["name"]
        constraints = [
            models.CheckConstraint(
                condition=~Q(parent=F("id")), name="catalog_category_not_own_parent"
            ),
            models.CheckConstraint(condition=~Q(name=""), name="catalog_category_name_not_empty"),
            models.UniqueConstraint(
                Lower("name"),
                condition=Q(parent__isnull=True),
                name="catalog_category_root_name",
            ),
            models.UniqueConstraint(
                F("parent"),
                Lower("name"),
                condition=Q(parent__isnull=False),
                name="catalog_category_child_name",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.parent.name} / {self.name}" if self.parent is not None else self.name


class UnitOfMeasure(BaseModel):
    """A unit (``UD``, ``M``, ``KG``…) and how many decimals its quantities may have.

    Seeded by ``catalog.0002``; read-only in the admin (a unit's ``decimals`` governs the
    quantities already entered, so it does not change under a material).
    """

    code = models.CharField(_("code"), max_length=10, unique=True)
    name = models.CharField(_("name"), max_length=50)
    decimals = models.PositiveSmallIntegerField(
        _("decimals"),
        default=0,
        validators=[MaxValueValidator(MAX_UNIT_DECIMALS)],
        help_text=_("How many decimal places a quantity in this unit may have."),
    )

    objects = UnitQuerySet.as_manager()

    class Meta:
        verbose_name = _("unit of measure")
        verbose_name_plural = _("units of measure")
        ordering = ["code"]
        constraints = [
            models.CheckConstraint(
                condition=Q(decimals__gte=0, decimals__lte=MAX_UNIT_DECIMALS),
                name="catalog_unit_decimals_range",
            ),
        ]

    def __str__(self) -> str:
        return self.code

    @property
    def label(self) -> str:
        return f"{self.code} · {self.name}"


class Material(BaseModel):
    """A catalog material. ``reference`` is the natural key, independent of the name.

    ``UNIQUE (id, unit_id)`` is the target of the unit-binding composite foreign keys of
    request and purchase-order lines (C9, plan §4.6): a referenced material's unit never
    changes. Deactivated, never deleted, once referenced.
    """

    reference = models.CharField(_("reference"), max_length=60, validators=[reference_validator])
    name = models.CharField(_("name"), max_length=200)
    description = models.TextField(_("description"), blank=True)
    category = models.ForeignKey(
        MaterialCategory,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="materials",
        verbose_name=_("category"),
    )
    unit = models.ForeignKey(
        UnitOfMeasure,
        on_delete=models.PROTECT,
        related_name="materials",
        verbose_name=_("unit of measure"),
    )
    manufacturer = models.CharField(_("manufacturer"), max_length=100, blank=True)
    manufacturer_ref = models.CharField(_("manufacturer reference"), max_length=100, blank=True)
    # NULL, not "", when absent: the format check and any future uniqueness skip it.
    ean = models.CharField(  # noqa: DJ001
        _("EAN / barcode"), max_length=14, null=True, blank=True, validators=[ean_validator]
    )
    is_stock_item = models.BooleanField(
        _("stock item"),
        default=False,
        help_text=_("Kept in the warehouse, so it can be handed over from stock."),
    )
    is_active = models.BooleanField(_("active"), default=True)

    objects = MaterialQuerySet.as_manager()

    class Meta:
        verbose_name = _("material")
        verbose_name_plural = _("materials")
        ordering = ["reference"]
        constraints = [
            models.UniqueConstraint(Lower("reference"), name="catalog_material_reference"),
            # The target of the C9 composite foreign keys.
            models.UniqueConstraint(fields=["id", "unit"], name="catalog_material_id_unit"),
            models.CheckConstraint(condition=~Q(reference=""), name="catalog_material_ref_set"),
            models.CheckConstraint(condition=~Q(name=""), name="catalog_material_name_set"),
            models.CheckConstraint(
                condition=Q(ean__isnull=True) | Q(ean__regex=EAN_PATTERN),
                name="catalog_material_ean_format",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.reference} · {self.name}"

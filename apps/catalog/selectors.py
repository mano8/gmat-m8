"""Reads for the catalog (plan §4.2, M3.1): every row through ``visible_to``.

The catalog is shared by the whole installation: ``catalog.view`` reaches all of it, and a
role without the capability gets no rows. Nothing here returns a price (the catalog holds
none; prices belong to purchase orders and need ``price.view``).

Search folds case and accents in the database (``catalog_fold``) and matches every term of
the query against the reference, the name or the category, so "cable 2,5" and
"DIFERENCIAL" find "Cable H07V-K 1x2,5 mm²" and "Interruptor diferencial". A decimal
number matches with either separator (``2,5`` or ``2.5``).
"""

import re
from uuid import UUID

from django.db.models import Case, Count, F, IntegerField, Q, QuerySet, Value, When
from django.db.models.fields.reverse_related import ManyToOneRel
from django.db.models.functions import Coalesce
from django.utils.translation import gettext_lazy as _

from apps.access.context import AccessContext
from apps.access.exceptions import NotInScope
from apps.access.roles import Capability
from apps.catalog.models import Material, MaterialCategory, UnitOfMeasure, fold

DEFAULT_PER_PAGE = 25
MATERIAL_SORTS = {"reference": ("reference",), "name": ("name",)}
MATERIAL_SORT_LABELS = {"reference": _("Reference"), "name": _("Name")}
SEARCH_LIMIT = 20
MAX_TERMS = 6
MAX_TERM_LENGTH = 60

VIEW = Capability.CATALOG_VIEW
ORG = Capability.ORG_MANAGE
UNIT_SORTS = {"code": ("code",), "name": ("name",), "decimals": ("decimals", "code")}
UNIT_SORT_LABELS = {"code": _("Code"), "name": _("Name"), "decimals": _("Decimals")}

STATE_ACTIVE, STATE_INACTIVE, STATE_ALL = "active", "inactive", "all"
STATES = (STATE_ACTIVE, STATE_INACTIVE, STATE_ALL)

_DECIMAL = re.compile(r"\d[.,]\d")


def categories(ctx: AccessContext) -> QuerySet[MaterialCategory]:
    """All categories, roots first with their children after them."""
    return (
        MaterialCategory.objects.visible_to(ctx, VIEW)
        .select_related("parent")
        .annotate(tree_key=Coalesce("parent__name", "name"))
        .order_by("tree_key", F("parent_id").asc(nulls_first=True), "name")
    )


def category(ctx: AccessContext, category_id: UUID) -> MaterialCategory:
    found = categories(ctx).filter(pk=category_id).first()
    if found is None:
        raise NotInScope(VIEW)
    return found


def root_categories(ctx: AccessContext) -> QuerySet[MaterialCategory]:
    """The categories a category can be placed under (two levels at most)."""
    return categories(ctx).filter(parent__isnull=True)


def units(ctx: AccessContext) -> QuerySet[UnitOfMeasure]:
    return UnitOfMeasure.objects.visible_to(ctx, VIEW).order_by("code")


def active_units(ctx: AccessContext, *, keep: UUID | None = None) -> QuerySet[UnitOfMeasure]:
    """The units a new material or an import may use: the active ones. ``keep`` adds the
    unit an existing material has, so editing it never drops its unit from the choices (Q72)."""
    rows = units(ctx)
    return (
        rows.filter(Q(is_active=True) | Q(pk=keep))
        if keep is not None
        else rows.filter(is_active=True)
    )


# --- units of measure, as group administrators manage them (M10.3, Q72) ---------------


def unit_list(ctx: AccessContext, *, q: str = "", state: str = "") -> QuerySet[UnitOfMeasure]:
    """Every unit, inactive ones included, filtered by text (code, name) and state, with the
    number of materials that have it (``material_count``)."""
    rows = UnitOfMeasure.objects.visible_to(ctx, ORG)
    if state == STATE_ACTIVE:
        rows = rows.filter(is_active=True)
    elif state == STATE_INACTIVE:
        rows = rows.filter(is_active=False)
    if q:
        rows = rows.filter(Q(code__icontains=q) | Q(name__icontains=q))
    return rows.annotate(material_count=Count("materials")).order_by("code")


def unit(ctx: AccessContext, unit_id: UUID) -> UnitOfMeasure:
    """A unit, inactive ones included; ``NotInScope`` (404) unless ``ctx`` holds ``org.manage``."""
    found = UnitOfMeasure.objects.visible_to(ctx, ORG).filter(pk=unit_id).first()
    if found is None:
        raise NotInScope(ORG)
    return found


def unit_in_use(of: UnitOfMeasure) -> bool:
    """Whether anything points at the unit: a material, a request line or its as-submitted
    copy, an order line. Worked out from the model's reverse relations, so a table that
    later gets a unit is counted without anyone remembering to add it here."""
    for relation in UnitOfMeasure._meta.get_fields(include_hidden=True):
        if isinstance(relation, ManyToOneRel):
            rows = relation.related_model._base_manager.filter(**{relation.field.name: of.pk})
            if rows.exists():
                return True
    return False


def _terms(query: str) -> list[str]:
    return [term[:MAX_TERM_LENGTH] for term in query.split()[:MAX_TERMS]]


def _variants(term: str) -> set[str]:
    """``2,5`` and ``2.5`` are one number: look for both spellings."""
    if _DECIMAL.search(term):
        return {term, term.replace(",", "."), term.replace(".", ",")}
    return {term}


def _matching_categories(ctx: AccessContext, term: str) -> list[UUID]:
    """Categories whose name has the term, and their subcategories (a handful of rows)."""
    found = list(
        categories(ctx)
        .annotate(folded=fold("name"))
        .filter(folded__contains=fold(Value(term)))
        .values_list("pk", flat=True)
    )
    if not found:
        return []
    children = MaterialCategory.objects.filter(parent_id__in=found).values_list("pk", flat=True)
    return [*found, *children]


def _text_filter(rows: QuerySet[Material], ctx: AccessContext, query: str) -> QuerySet[Material]:
    """Rows where every term of ``query`` is in the reference, the name or the category."""
    terms = _terms(query)
    if not terms:
        return rows
    rows = rows.annotate(reference_folded=fold("reference"), name_folded=fold("name"))
    for term in terms:
        condition = Q(pk__in=[])
        for variant in _variants(term):
            needle = fold(Value(variant))
            condition |= Q(reference_folded__contains=needle) | Q(name_folded__contains=needle)
        category_ids = [
            pk for variant in _variants(term) for pk in _matching_categories(ctx, variant)
        ]
        if category_ids:
            condition |= Q(category_id__in=category_ids)
        rows = rows.filter(condition)
    return rows


def _relevance(rows: QuerySet[Material], query: str) -> QuerySet[Material]:
    """The exact reference first, then references and names that start like the query."""
    whole = fold(Value(" ".join(_terms(query))))
    rank = Case(
        When(reference_folded=whole, then=Value(0)),
        When(reference_folded__startswith=whole, then=Value(1)),
        When(name_folded__startswith=whole, then=Value(2)),
        default=Value(3),
        output_field=IntegerField(),
    )
    return rows.annotate(relevance=rank).order_by("relevance", "reference")


def materials(
    ctx: AccessContext,
    *,
    q: str = "",
    category_id: UUID | None = None,
    state: str = STATE_ACTIVE,
) -> QuerySet[Material]:
    """The materials ``ctx`` may view, filtered by text, category (and its subcategories)
    and state, in reference order."""
    rows: QuerySet[Material] = Material.objects.visible_to(ctx, VIEW).select_related(
        "unit", "category__parent"
    )
    if state == STATE_ACTIVE:
        rows = rows.filter(is_active=True)
    elif state == STATE_INACTIVE:
        rows = rows.filter(is_active=False)
    if category_id is not None:
        rows = rows.filter(Q(category_id=category_id) | Q(category__parent_id=category_id))
    return _text_filter(rows, ctx, q).order_by("reference")


def search(
    ctx: AccessContext, query: str, *, limit: int = SEARCH_LIMIT, unit_id: UUID | None = None
) -> list[Material]:
    """The best ``limit`` active materials for ``query`` (the picker of request lines),
    optionally only those measured in ``unit_id`` (filtered before the limit).

    Never returns a price. A query of nothing but spaces finds nothing.
    """
    if not _terms(query):
        return []
    rows: QuerySet[Material] = Material.objects.visible_to(ctx, VIEW).filter(is_active=True)
    if unit_id is not None:
        rows = rows.filter(unit_id=unit_id)
    rows = _text_filter(rows, ctx, query).select_related("unit", "category__parent")
    return list(_relevance(rows, query)[:limit])


def material(ctx: AccessContext, material_id: UUID) -> Material:
    """A material ``ctx`` may view, inactive ones included (documents keep pointing at them)."""
    found = (
        Material.objects.visible_to(ctx, VIEW)
        .select_related("unit", "category__parent")
        .filter(pk=material_id)
        .first()
    )
    if found is None:
        raise NotInScope(VIEW)
    return found


def manageable(ctx: AccessContext, of: Material | None = None) -> bool:
    """Whether ``ctx`` may add or change materials (the services check again)."""
    if of is None:
        return ctx.has(Capability.CATALOG_MANAGE)
    return ctx.can(Capability.CATALOG_MANAGE, of)


def units_by_code(ctx: AccessContext) -> dict[str, UnitOfMeasure]:
    """The units, keyed by upper-case code (for imports)."""
    return {unit.code.upper(): unit for unit in units(ctx)}


def material_by_reference(ctx: AccessContext, reference: str) -> Material | None:
    """The material with this reference, ignoring case (its natural key), if viewable."""
    return Material.objects.visible_to(ctx, VIEW).filter(reference__iexact=reference).first()


def category_named(
    ctx: AccessContext, name: str, parent_id: UUID | None = None
) -> MaterialCategory | None:
    """The category with this name under ``parent_id`` (a root when ``None``), ignoring case."""
    return categories(ctx).filter(name__iexact=name, parent_id=parent_id).first()

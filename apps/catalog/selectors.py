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

from django.db.models import Case, F, IntegerField, Q, QuerySet, Value, When
from django.db.models.functions import Coalesce

from apps.access.context import AccessContext
from apps.access.exceptions import NotInScope
from apps.access.roles import Capability
from apps.catalog.models import Material, MaterialCategory, UnitOfMeasure, fold

PAGE_SIZE = 25
SEARCH_LIMIT = 20
MAX_TERMS = 6
MAX_TERM_LENGTH = 60

VIEW = Capability.CATALOG_VIEW

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


def search(ctx: AccessContext, query: str, *, limit: int = SEARCH_LIMIT) -> list[Material]:
    """The best ``limit`` active materials for ``query`` (the picker of request lines).

    Never returns a price. A query of nothing but spaces finds nothing.
    """
    if not _terms(query):
        return []
    rows: QuerySet[Material] = Material.objects.visible_to(ctx, VIEW).filter(is_active=True)
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

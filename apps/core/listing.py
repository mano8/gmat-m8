"""The shared list toolkit (plan M7d.1, Q39-Q42): page, page size and sort of every list.

A list view reads its filters with its own form, takes the narrowed, scoped queryset from a
selector and hands it to :func:`paginate`; the template renders the page with
``components/list_toolbar.html``, ``components/list_summary.html`` and
``components/pagination_legacy.html``. The three parameters live in the URL only (``page``,
``per_page``, ``sort``): bookmarkable, nothing stored per user. A value that is not valid
falls back to the default, never to a 400, 404 or 500.
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from django import forms
from django.core.exceptions import ImproperlyConfigured
from django.core.paginator import Page, Paginator
from django.db.models import F, QuerySet
from django.http import HttpRequest, HttpResponse
from django.shortcuts import render
from django.utils.cache import patch_vary_headers
from django.utils.functional import Promise
from django.utils.translation import gettext as _
from django.utils.translation import gettext_lazy

from apps.core.http import is_htmx

PAGE_SIZES = (10, 25, 50, 100)
"""The page sizes a user may pick (Q39); the largest is the maximum."""

_PAGE, _PER_PAGE, _SORT = "page", "per_page", "sort"

SortKeys = Mapping[str, tuple[str, ...]]
"""Sort key (the value of ``?sort=``) → the ``order_by`` fields it stands for, ascending."""

_PK = "pk"


class ListParams(forms.Form):
    """``page``, ``per_page`` and ``sort`` of a list request; an invalid one is dropped."""

    page = forms.IntegerField(required=False, min_value=1)
    per_page = forms.TypedChoiceField(
        required=False, coerce=int, choices=[(size, str(size)) for size in PAGE_SIZES]
    )
    sort = forms.CharField(required=False, max_length=40)


@dataclass(frozen=True)
class ListScope:
    """Where a list keeps its parameters when it is not the only one on the page (M7d.3).

    A screen with several paged sections gives each a ``prefix`` (``drafts_`` makes
    ``drafts_page``), so every section pages on its own; ``anchor`` is the element id the
    pager links return to; ``sized`` is false for a section whose page size is fixed.
    """

    prefix: str = ""
    anchor: str = ""
    sized: bool = True

    @property
    def page_param(self) -> str:
        return f"{self.prefix}{_PAGE}"

    @property
    def per_page_param(self) -> str:
        return f"{self.prefix}{_PER_PAGE}"

    @property
    def sort_param(self) -> str:
        return f"{self.prefix}{_SORT}"


WHOLE_PAGE = ListScope()
"""The scope of the one list of a page: ``page``, ``per_page`` and ``sort``."""


@dataclass(frozen=True)
class ListPage:
    """One page of a list, with what the toolbar and the pager show."""

    page: Page[Any]
    per_page: int
    sort: str
    """The effective sort: a key, ``-key`` when descending, empty for the queryset's own order."""
    scope: ListScope = WHOLE_PAGE
    sort_options: tuple[tuple[str, str], ...] = ()
    """``(value, label)`` for the phone-size "sort by" select; empty when the list has no sort."""

    @property
    def sizes(self) -> tuple[int, ...]:
        """The page sizes offered; none when the size of this list is fixed."""
        return PAGE_SIZES if self.scope.sized else ()

    @property
    def page_param(self) -> str:
        return self.scope.page_param

    @property
    def per_page_param(self) -> str:
        return self.scope.per_page_param

    @property
    def sort_param(self) -> str:
        return self.scope.sort_param

    @property
    def anchor(self) -> str:
        return self.scope.anchor

    @property
    def rows(self) -> Sequence[Any]:
        return list(self.page.object_list)

    @property
    def total(self) -> int:
        return self.page.paginator.count

    @property
    def first(self) -> int:
        """Position of the first row on the page (0 for an empty list)."""
        return self.page.start_index()

    @property
    def last(self) -> int:
        return self.page.end_index()

    @property
    def has_other_pages(self) -> bool:
        return self.page.has_other_pages()

    @property
    def number(self) -> int:
        return self.page.number

    @property
    def num_pages(self) -> int:
        return self.page.paginator.num_pages

    @property
    def pages(self) -> list[int | None]:
        """The numbered window (the ends, one page on each side of this one); ``None`` is a gap."""
        window = self.page.paginator.get_elided_page_range(self.number, on_each_side=1, on_ends=1)
        return [item if isinstance(item, int) else None for item in window]


def _sort_fields(sorts: SortKeys, wanted: str) -> tuple[str, tuple[str, ...]] | None:
    """The effective sort string and its fields for ``wanted`` (``key`` or ``-key``), if allowed."""
    key = wanted.removeprefix("-")
    fields = sorts.get(key)
    if fields is None:
        return None
    if not wanted.startswith("-"):
        return wanted, fields
    return wanted, tuple(
        field.removeprefix("-") if field.startswith("-") else f"-{field}" for field in fields
    )


def _sort_options(labels: Mapping[str, str | Promise] | None) -> tuple[tuple[str, str], ...]:
    if not labels:
        return ()
    options = [("", _("Default order"))]
    for key, label in labels.items():
        options.append((key, _("%(label)s (ascending)") % {"label": label}))
        options.append((f"-{key}", _("%(label)s (descending)") % {"label": label}))
    return tuple((value, str(text)) for value, text in options)


def _total_order(rows: QuerySet[Any], fields: tuple[str, ...]) -> QuerySet[Any]:
    """``rows`` in ``fields`` (or its own order) with the primary key as the last tie-breaker."""
    if fields:
        return rows.order_by(*fields, _PK)
    if not rows.ordered:
        raise ImproperlyConfigured(
            "A list needs an order to be paged: order the queryset or give a default sort."
        )
    own = rows.query.order_by or tuple(rows.model._meta.ordering)
    return rows.order_by(*own, _PK)


def _scoped_query(request: HttpRequest, scope: ListScope) -> dict[str, str]:
    """The three list parameters of ``scope`` under the names :class:`ListParams` knows."""
    names = {
        _PAGE: scope.page_param,
        _PER_PAGE: scope.per_page_param,
        _SORT: scope.sort_param,
    }
    return {name: request.GET[wanted] for name, wanted in names.items() if wanted in request.GET}


def paginate(
    request: HttpRequest,
    rows: QuerySet[Any] | Sequence[Any],
    *,
    default_per_page: int,
    sorts: SortKeys | None = None,
    default_sort: str = "",
    sort_labels: Mapping[str, str | Promise] | None = None,
    scope: ListScope = WHOLE_PAGE,
) -> ListPage:
    """The page of ``rows`` that ``?page=``, ``?per_page=`` and ``?sort=`` ask for.

    ``sorts`` is the allowlist of sort keys of this list; an unknown key, like any invalid
    value, falls back to ``default_sort`` (the order the list has today; empty when the
    queryset's own order is not one of the keys) and to ``default_per_page``. A queryset is
    always paged in a total order: the primary key follows whatever order it has.
    ``sort_labels`` (key → label) feeds the toolbar's "sort by" select for phone widths, where
    the column headers are not shown. ``scope`` renames the three parameters for a section of
    a page that has several lists.
    """
    if default_per_page not in PAGE_SIZES:
        raise ImproperlyConfigured(f"default_per_page must be one of {PAGE_SIZES}.")
    params = ListParams(_scoped_query(request, scope))
    params.is_valid()  # invalid fields are dropped from cleaned_data: the defaults apply
    data = params.cleaned_data
    per_page = (data.get("per_page") if scope.sized else None) or default_per_page
    sort: str = ""
    fields: tuple[str, ...] = ()
    for wanted in (data.get("sort") or "", default_sort):
        chosen = _sort_fields(sorts or {}, wanted)
        if chosen:
            sort, fields = chosen
            break
    if isinstance(rows, QuerySet):
        rows = _total_order(rows, fields)
    page = Paginator(rows, per_page).get_page(data.get("page") or 1)
    return ListPage(
        page=page,
        per_page=per_page,
        sort=sort,
        scope=scope,
        sort_options=_sort_options(sort_labels),
    )


def render_list(
    request: HttpRequest, template: str, context: dict[str, Any], *, partial: str = "results"
) -> HttpResponse:
    """The list page, or only its ``results`` partial for a real htmx request.

    The answer depends on ``HX-Request``, so it says ``Vary: HX-Request``: no cache may hand
    the fragment to a plain navigation.
    """
    name = f"{template}#{partial}" if is_htmx(request) else template
    response = render(request, name, context)
    patch_vary_headers(response, ["HX-Request"])
    return response


ACTIVE, REVOKED = "active", "revoked"
REVOCATION_CHOICES = [
    ("", gettext_lazy("All")),
    (ACTIVE, gettext_lazy("Active")),
    (REVOKED, gettext_lazy("Revoked")),
]
"""The state filter of a section listing grants that are revoked, never deleted."""


OPEN_FIRST = F("revoked_at").asc(nulls_first=True)
"""Order of a grant list: the ones still in force first (a plain ``revoked_at`` puts the NULLs
last on PostgreSQL), then the revoked."""


def by_revocation(rows: QuerySet[Any], state: str) -> QuerySet[Any]:
    """``rows`` that are not revoked (``active``) or that are (``revoked``); any other value
    leaves them all."""
    if state == ACTIVE:
        return rows.filter(revoked_at__isnull=True)
    if state == REVOKED:
        return rows.filter(revoked_at__isnull=False)
    return rows


def active_filters(form: forms.Form) -> list[str]:
    """Names of the form's fields that carry a value in the request (the filters in force)."""
    if not form.is_bound:
        return []
    return [name for name in form.fields if form.data.get(form.add_prefix(name))]

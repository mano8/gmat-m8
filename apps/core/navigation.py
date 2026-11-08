"""The signed-in navigation (M8b.2): categories of entries, the phone bar and the active entry.

One definition feeds the left menu, the phone's bottom bar, its "More" sheet and the top bar's
breadcrumb. An entry shows only to someone holding its capability (``ctx.has``, the rule of
``{% can %}``): hiding is a courtesy and never the control, views check with ``require`` and
the selectors' scopes. A category with nothing reachable is absent.
"""

from collections.abc import Callable
from dataclasses import dataclass

from django.http import HttpRequest
from django.urls import ResolverMatch, reverse
from django.utils.functional import Promise
from django.utils.translation import gettext_lazy as _

from apps.access.middleware import access_for
from apps.core.preferences import CATEGORY_KEYS

#: The bottom bar holds this many entries and a "More" button (the maintainer's four plus More).
PHONE_BAR_SIZE = 4

type Active = Callable[[ResolverMatch], bool]


def _app(*names: str, but: tuple[str, ...] = ()) -> Active:
    """Active on every page of the given apps, except the named URLs."""
    return lambda match: match.app_name in names and match.url_name not in but


def _url(*names: str) -> Active:
    return lambda match: match.view_name in names


@dataclass(frozen=True)
class Entry:
    """One place in the menu. ``rank`` orders the phone bar: the lowest reachable come first."""

    key: str
    label: str | Promise
    url_name: str
    icon: str
    capability: str | None
    active: Active
    rank: int


@dataclass(frozen=True)
class Category:
    key: str
    label: str | Promise
    entries: tuple[Entry, ...]


CATEGORIES: tuple[Category, ...] = (
    Category(
        "work",
        _("Work"),
        (
            Entry("home", _("Home"), "core:home", "house", None, _url("core:home"), 1),
            Entry(
                "requests",
                _("Requests"),
                "requests:list",
                "clipboard-list",
                "request.view",
                _app("requests", but=("inbox",)),
                2,
            ),
            Entry(
                "inbox",
                _("Inbox"),
                "requests:inbox",
                "inbox",
                "request.review",
                _url("requests:inbox"),
                3,
            ),
        ),
    ),
    Category(
        "purchasing",
        _("Purchasing"),
        (
            Entry(
                "worklist",
                _("To purchase"),
                "purchasing:worklist",
                "shopping-cart",
                "po.manage",
                _url("purchasing:worklist", "purchasing:new"),
                4,
            ),
            Entry(
                "orders",
                _("Orders"),
                "purchasing:orders",
                "truck",
                "po.view",
                _app("purchasing", but=("worklist", "new")),
                7,
            ),
        ),
    ),
    Category(
        "site",
        _("Site & warehouse"),
        (
            Entry(
                "receiving",
                _("Receiving"),
                "receiving:list",
                "package-check",
                "receipt.view",
                _app("receiving", but=("projects", "expected")),
                5,
            ),
            Entry(
                "expected",
                _("Expected deliveries"),
                "receiving:projects",
                "calendar",
                "receipt.view",
                _url("receiving:projects", "receiving:expected"),
                8,
            ),
            Entry(
                "handovers",
                _("Handovers"),
                "handovers:list",
                "hand-helping",
                "handover.view",
                _app("handovers"),
                6,
            ),
        ),
    ),
    Category(
        "organization",
        _("Organization"),
        (
            Entry(
                "projects",
                _("Projects"),
                "projects:list",
                "folder",
                "project.view",
                _app("projects"),
                9,
            ),
            Entry("people", _("People"), "org:people", "users", "org.manage", _app("org"), 10),
            Entry(
                "catalog",
                _("Catalog"),
                "catalog:list",
                "package",
                "catalog.view",
                _app("catalog"),
                11,
            ),
            Entry(
                "suppliers",
                _("Suppliers"),
                "suppliers:list",
                "building",
                "supplier.view",
                _app("suppliers"),
                12,
            ),
        ),
    ),
    Category(
        "insights",
        _("Insights"),
        (
            Entry(
                "audit", _("Audit"), "audit:list", "shield-check", "audit.view", _app("audit"), 13
            ),
            Entry(
                "exports",
                _("Exports"),
                "exports:index",
                "download",
                "export.csv",
                _app("exports"),
                14,
            ),
        ),
    ),
)

assert tuple(category.key for category in CATEGORIES) == CATEGORY_KEYS  # noqa: S101


@dataclass(frozen=True)
class NavEntry:
    """An entry as a page shows it: where it goes and whether it is the current one."""

    key: str
    label: str | Promise
    url: str
    icon: str
    current: bool


@dataclass(frozen=True)
class NavGroup:
    """A category as a page shows it. ``open`` is the remembered state, except that the group of
    the current page is always open."""

    key: str
    label: str | Promise
    entries: tuple[NavEntry, ...]
    open: bool
    saved_open: bool


@dataclass(frozen=True)
class Navigation:
    groups: tuple[NavGroup, ...]
    bar: tuple[NavEntry, ...]
    more: tuple[NavGroup, ...]
    here: tuple[str | Promise, str | Promise] | None = None


def _reachable(request: HttpRequest) -> list[tuple[Category, list[Entry]]]:
    ctx = access_for(request)
    reachable = []
    for category in CATEGORIES:
        entries = [e for e in category.entries if e.capability is None or ctx.has(e.capability)]
        if entries:
            reachable.append((category, entries))
    return reachable


def _entry(entry: Entry, match: ResolverMatch | None) -> NavEntry:
    return NavEntry(
        key=entry.key,
        label=entry.label,
        url=reverse(entry.url_name),
        icon=entry.icon,
        current=match is not None and entry.active(match),
    )


def _group(category: Category, entries: list[NavEntry], folded: frozenset[str]) -> NavGroup:
    saved_open = category.key not in folded
    current = any(entry.current for entry in entries)
    return NavGroup(category.key, category.label, tuple(entries), saved_open or current, saved_open)


def _phone_bar_keys(reachable: list[tuple[Category, list[Entry]]]) -> list[str]:
    """The first entries the user can reach in the fixed rank order (Home first)."""
    ranked = sorted((entry for _c, entries in reachable for entry in entries), key=lambda e: e.rank)
    return [entry.key for entry in ranked[:PHONE_BAR_SIZE]]


def _here(groups: tuple[NavGroup, ...]) -> tuple[str | Promise, str | Promise] | None:
    """The category and the entry of the current page, for the top bar."""
    for group in groups:
        for entry in group.entries:
            if entry.current:
                return group.label, entry.label
    return None


def _more(
    shown: list[tuple[Category, list[NavEntry]]], on_bar: list[str], folded: frozenset[str]
) -> tuple[NavGroup, ...]:
    """The categories again without the entries on the bar (the More sheet); empty ones leave."""
    groups = []
    for category, entries in shown:
        rest = [entry for entry in entries if entry.key not in on_bar]
        if rest:
            groups.append(_group(category, rest, folded))
    return tuple(groups)


def navigation(request: HttpRequest, folded: frozenset[str]) -> Navigation:
    """The menu for this request: what the user can reach, what is current, what is folded."""
    match = request.resolver_match
    reachable = _reachable(request)
    shown = [(c, [_entry(entry, match) for entry in entries]) for c, entries in reachable]
    on_bar = _phone_bar_keys(reachable)
    groups = tuple(_group(category, entries, folded) for category, entries in shown)
    by_key = {entry.key: entry for group in groups for entry in group.entries}
    return Navigation(
        groups=groups,
        bar=tuple(by_key[key] for key in on_bar),
        more=_more(shown, on_bar, folded),
        here=_here(groups),
    )

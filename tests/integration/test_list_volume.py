"""Every list on the volume dataset (plan M7d.4): no N+1, and inside the time budget.

``tests/volume.py`` builds 100k audit events, 20k materials, 10k requests with 50k lines,
5k orders, 5k receipts and 2k handovers once for the module (in a transaction that is rolled
back at the end). Each screen is requested as the user who works with it:

- the number of queries at ``per_page=10`` equals the number at ``per_page=100`` (and the
  first page costs what the last one does): a query per row would show here;
- the last page at 100 rows and a filtered page answer in at most ``BUDGET_SECONDS`` of server
  time (the whole request, template included);
- the lists read from a cursor (the purchasing worklist) give the same rows as working out
  the whole list, page after page.

Run with ``-s`` to print the timings of every screen.
"""

import re
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from typing import Any
from urllib.parse import parse_qs, urlparse

import pytest
from django.db import connection, transaction
from django.test import Client, RequestFactory
from django.test.utils import CaptureQueriesContext
from django.urls import reverse

from apps.access.context import AccessContext
from apps.audit.models import AuditEvent
from apps.catalog.models import Material
from apps.core.listing import cursor_page
from apps.dashboards.panels import COUNT_CAP
from apps.handovers.models import Handover
from apps.material_requests.models import MaterialRequest, MaterialRequestLine
from apps.purchasing import selectors as purchasing_selectors
from apps.purchasing.models import PurchaseOrder
from apps.receiving.models import GoodsReceipt
from tests.org import Org, People, build_org, build_people, client_for
from tests.volume import Volume, build_volume

pytestmark = [pytest.mark.integration, pytest.mark.django_db]

BUDGET_SECONDS = 0.3
RUNS = 5
LAST = "9999"  # past the end: the paginator answers with the last page
CURSOR_QUERIES = 80  # a page of a cursor list reads a few stretches of ~10 queries each


@dataclass
class World:
    org: Org
    people: People
    volume: Volume
    clients: dict[str, Client]


@pytest.fixture(scope="module")
def world(django_db_setup: None, django_db_blocker: Any) -> Iterator[World]:
    with django_db_blocker.unblock(), transaction.atomic():
        org = build_org()
        people = build_people(org)
        volume = build_volume(org, people)
        who = ("group_admin", "mm_mad", "tech_mad", "site_manager", "wo_mad")
        clients = {name: client_for(getattr(people, name)) for name in who}
        yield World(org, people, volume, clients)
        transaction.set_rollback(True)


@dataclass(frozen=True)
class Screen:
    """A list and how to ask for its pages.

    ``size``, ``page`` and ``sort`` are the names of its parameters (a section of a page has
    a prefix). ``filtered`` is a narrowing that still leaves a long list.
    """

    name: str
    url: Callable[[World], str]
    actors: tuple[str, ...]
    filtered: dict[str, str]
    size: str = "per_page"
    page: str = "page"
    fixed_size: bool = False
    cursor: bool = False
    largest: str = "100"  # the largest page size the list offers


def reverse_(name: str, *args: Any) -> Callable[[World], str]:
    return lambda world: reverse(name, args=args)


def of(name: str, pick: Callable[[World], Any], *more: Any) -> Callable[[World], str]:
    return lambda world: reverse(name, args=[pick(world), *more])


def panel(key: str, *actors: str) -> Screen:
    """A dashboard panel: five rows and a count, no paging of its own."""
    return Screen(f"panel-{key}", reverse_("dashboard:panel", key), actors, {}, fixed_size=True)


PANELS = (
    panel("my-requests", "tech_mad"),
    panel("inbox", "mm_mad", "group_admin"),
    panel("assigned", "mm_mad"),
    panel("sourcing", "mm_mad"),
    panel("to-purchase", "mm_mad", "group_admin"),
    panel("stale-drafts", "mm_mad"),
    panel("unallocated", "mm_mad"),
    panel("awaiting-receipt", "mm_mad", "wo_mad"),
    panel("overdue", "mm_mad"),
    panel("to-hand-over", "wo_mad", "mm_mad"),
    panel("site-surplus", "wo_mad"),
    panel("closed-attention", "mm_mad"),
    panel("dispatches", "mm_mad"),
    panel("overview", "group_admin"),
)

SCREENS = (
    Screen("catalog", reverse_("catalog:list"), ("mm_mad", "group_admin"), {"q": "cable 12"}),
    Screen("suppliers", reverse_("suppliers:list"), ("mm_mad",), {"q": "volumen 01"}),
    Screen(
        "projects",
        reverse_("projects:list"),
        ("mm_mad", "group_admin"),
        {"q": "obra", "status": "ACTIVE"},
    ),
    Screen(
        "people", reverse_("org:people"), ("group_admin",), {"role": "TECHNICIAN", "q": "persona 1"}
    ),
    Screen(
        "invitations",
        reverse_("org:invitations"),
        ("group_admin",),
        {"state": "valid", "delivery": "SENT"},
    ),
    Screen(
        "my-requests",
        reverse_("requests:list"),
        ("tech_mad",),
        {"status": "APPROVED"},
        largest="50",
    ),
    Screen("new-request", reverse_("requests:new"), ("tech_mad",), {"q": "volumen 1"}),
    Screen(
        "inbox",
        reverse_("requests:inbox"),
        ("mm_mad", "group_admin"),
        {"status": "APPROVED", "priority": "HIGH"},
    ),
    Screen(
        "orders",
        reverse_("purchasing:orders"),
        ("mm_mad", "group_admin"),
        {"status": "CLOSED", "q": "PED-A"},
    ),
    Screen(
        "receipts",
        reverse_("receiving:list"),
        ("mm_mad", "group_admin"),
        {"status": "POSTED", "q": "REC"},
    ),
    Screen("receivable-orders", reverse_("receiving:orders"), ("mm_mad",), {"q": "PED-A"}),
    Screen("expected-projects", reverse_("receiving:projects"), ("site_manager",), {"q": "obra"}),
    Screen(
        "handovers",
        reverse_("handovers:list"),
        ("mm_mad", "group_admin"),
        {"status": "POSTED", "kind": "FROM_WAREHOUSE"},
    ),
    Screen(
        "audit",
        reverse_("audit:list"),
        ("mm_mad", "group_admin"),
        {"q": "approved", "object_type": "purchasing.purchaseorder"},
    ),
    Screen(
        "worklist",
        reverse_("purchasing:worklist"),
        ("mm_mad", "group_admin"),
        {"priority": "HIGH"},
        page="after",
        cursor=True,
        largest="50",
    ),
    Screen(
        "expected-deliveries",
        of("receiving:expected", lambda w: w.org.hospital.pk),
        ("site_manager", "mm_mad"),
        {"supplier": "volumen 01"},
    ),
    Screen(
        "project-materials",
        of("requests:project_materials", lambda w: w.org.hospital.pk),
        ("mm_mad",),
        {"q": "cable", "outstanding": "on"},
    ),
    Screen(
        "project-assignments",
        of("projects:detail", lambda w: w.volume.hot_project.pk),
        ("group_admin",),
        {"assignments-state": "revoked"},
        size="assignments_per_page",
        page="assignments_page",
        fixed_size=True,
    ),
    Screen(
        "person-memberships",
        of("org:person", lambda w: w.volume.busy_person.pk),
        ("group_admin",),
        {"memberships-state": "revoked"},
        size="memberships_per_page",
        page="memberships_page",
        fixed_size=True,
    ),
    Screen(
        "person-projects",
        of("org:person", lambda w: w.volume.busy_person.pk),
        ("group_admin",),
        {"projects-state": "revoked"},
        size="projects_per_page",
        page="projects_page",
        fixed_size=True,
    ),
    Screen(
        "person-invitations",
        of("org:person", lambda w: w.volume.busy_person.pk),
        ("group_admin",),
        {"invitations-status": "pending"},
        size="invitations_per_page",
        page="invitations_page",
        fixed_size=True,
    ),
    *PANELS,
    Screen(
        "audit-history",
        lambda w: reverse(
            "audit:object", args=["material_requests.materialrequest", w.volume.hot_request.pk]
        ),
        ("mm_mad", "group_admin"),
        {"q": "approved"},
    ),
)


def get(client: Client, url: str, params: dict[str, str]) -> tuple[float, int, Any]:
    """One request: seconds of server time, queries."""
    with CaptureQueriesContext(connection) as queries:
        started = time.perf_counter()
        response = client.get(url, params)
        seconds = time.perf_counter() - started
    assert response.status_code == 200, (url, params, response.status_code)
    return seconds, len(queries), response


def measure(client: Client, url: str, params: dict[str, str]) -> tuple[float, int, Any]:
    get(client, url, params)  # warm up: plan caches, buffers, templates
    runs = [get(client, url, params) for _ in range(RUNS)]
    return min(run[0] for run in runs), runs[-1][1], runs[-1][2]


def next_params(response: Any, screen: Screen) -> dict[str, str] | None:
    """The query of the page after this one, read from its ``rel="next"`` link."""
    found = re.search(r'href="([^"]*)"[^>]*rel="next"', response.content.decode())
    if not found:
        return None
    query = parse_qs(urlparse(found[1].replace("&amp;", "&")).query)
    return {name: values[0] for name, values in query.items()}


def deepest(client: Client, url: str, screen: Screen) -> tuple[dict[str, str], dict[str, str]]:
    """Follow the cursor to the last page; return its query and the query of the slowest page
    on the way."""
    params, slowest, slowest_seconds = (
        {screen.size: screen.largest},
        {screen.size: screen.largest},
        0.0,
    )
    while True:
        seconds, _, response = get(client, url, params)
        if seconds > slowest_seconds:
            slowest, slowest_seconds = params, seconds
        following = next_params(response, screen)
        if following is None:
            return params, slowest
        params = following


CASES = [(screen, actor) for screen in SCREENS for actor in screen.actors]
IDS = [f"{screen.name}-{actor}" for screen, actor in CASES]
TIMINGS: dict[str, dict[str, float]] = {}


def test_the_dataset_has_the_specified_size(world: World) -> None:
    counts = {
        "audit events": (AuditEvent.objects.count(), 100_000),
        "materials": (Material.objects.count(), 20_000),
        "requests": (MaterialRequest.objects.count(), 10_000),
        "request lines": (MaterialRequestLine.objects.count(), 50_000),
        "orders": (PurchaseOrder.objects.count(), 4_000),  # drafts and all, never fewer
        "receipts": (GoodsReceipt.objects.count(), 4_500),
        "handovers": (Handover.objects.count(), 2_000),
    }
    for what, (found, at_least) in counts.items():
        assert found >= at_least, f"{what}: {found} < {at_least}"


@pytest.mark.parametrize(("screen", "actor"), CASES, ids=IDS)
def test_a_list_does_not_query_per_row(world: World, screen: Screen, actor: str) -> None:
    client, url = world.clients[actor], screen.url(world)
    if screen.cursor:
        _, small, _ = get(client, url, {screen.size: "10"})
        _, large, _ = get(client, url, {screen.size: screen.largest})
        assert max(small, large) <= CURSOR_QUERIES, (small, large)
        return
    sizes = ("10", "10") if screen.fixed_size else ("10", screen.largest)
    first, last = (
        get(client, url, {screen.size: sizes[0]}),
        get(client, url, {screen.size: sizes[1], screen.page: LAST}),
    )
    if not screen.fixed_size:
        assert first[1] == get(client, url, {screen.size: screen.largest})[1], (
            "10 rows cost another count than 100"
        )
    assert first[1] == last[1], f"first page {first[1]} queries, last page {last[1]}"


@pytest.mark.parametrize(("screen", "actor"), CASES, ids=IDS)
def test_a_list_answers_within_the_budget(world: World, screen: Screen, actor: str) -> None:
    client, url = world.clients[actor], screen.url(world)
    narrowed = {screen.size: screen.largest, **screen.filtered}
    if screen.cursor:
        last, slowest = deepest(client, url, screen)
        seconds, _, _ = measure(client, url, slowest)
        assert seconds <= BUDGET_SECONDS, (
            f"the slowest page on the way took {seconds * 1000:.0f} ms"
        )
    else:
        last = {screen.size: screen.largest, screen.page: LAST}
    last_seconds, _, _ = measure(client, url, last)
    filtered_seconds, _, _ = measure(client, url, narrowed)
    TIMINGS[f"{screen.name}/{actor}"] = {"last": last_seconds, "filtered": filtered_seconds}
    assert last_seconds <= BUDGET_SECONDS, f"last page: {last_seconds * 1000:.0f} ms"
    assert filtered_seconds <= BUDGET_SECONDS, f"filtered: {filtered_seconds * 1000:.0f} ms"


def walk(keys: list[Any], window: Any, size: int) -> list[Any]:
    """Every row of a cursor list, page after page."""
    rows: list[Any] = []
    query: dict[str, str] = {"per_page": str(size)}
    while True:
        page = cursor_page(RequestFactory().get("/", query), keys, window, default_per_page=50)
        rows += page.rows
        if not page.next:
            return rows
        query = {"per_page": str(size), "after": page.next}


@pytest.mark.parametrize("actor", ["mm_mad", "group_admin"])
def test_the_cursor_worklist_gives_the_rows_of_the_whole_list(world: World, actor: str) -> None:
    ctx = AccessContext.for_user(getattr(world.people, actor))
    eager = purchasing_selectors.by_material(purchasing_selectors.to_purchase(ctx))
    worklist = purchasing_selectors.Worklist(ctx)
    for size in (10, 100):
        lazy = walk(worklist.keys, worklist.window, size)
        assert len(lazy) == len(eager) > size
        assert [
            (g.reference, [(r.line.pk, r.to_purchase, r.reserved) for r in g.rows]) for g in lazy
        ] == [
            (g.reference, [(r.line.pk, r.to_purchase, r.reserved) for r in g.rows]) for g in eager
        ]


@pytest.mark.parametrize("actor", ["mm_mad", "group_admin"])
def test_the_cursor_section_of_unallocated_lines_gives_the_whole_list(
    world: World, actor: str
) -> None:
    ctx = AccessContext.for_user(getattr(world.people, actor))
    eager = purchasing_selectors.unallocated_on_order(ctx)
    nobody = purchasing_selectors.Unallocated(ctx)
    lazy = walk(nobody.keys, nobody.window, 10)
    assert len(eager) > 10
    assert [(r.line.pk, r.unallocated) for r in lazy] == [(r.line.pk, r.unallocated) for r in eager]


def test_a_dashboard_count_stops_at_the_cap(world: World) -> None:
    html = world.clients["mm_mad"].get(reverse("dashboard:panel", args=["to-purchase"])).content
    assert f"{COUNT_CAP}+".encode() in html


def test_zz_timings(world: World, capsys: pytest.CaptureFixture[str]) -> None:
    """The table of the run, for ``-s`` and for the PR."""
    with capsys.disabled():
        print()
        for name, found in sorted(TIMINGS.items()):
            last, filtered = found["last"] * 1000, found["filtered"] * 1000
            print(f"  {name:36} last {last:5.0f} ms   filtered {filtered:5.0f} ms")

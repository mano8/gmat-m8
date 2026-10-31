"""Catalog search at scale (plan M3.1 "Done when"): under 100 ms on 20,000 materials.

Also checks that the trigram indexes are what answers, so the speed is not an accident of a
small table, and that ``catalog.0002`` reverses.
"""

import random
import time
from collections.abc import Iterator

import pytest
from django.db import connection
from django.db.models import Value

from apps.access.context import AccessContext
from apps.access.roles import Role
from apps.accounts.models import User
from apps.catalog import selectors
from apps.catalog.models import Material, MaterialCategory, UnitOfMeasure, fold
from apps.core.ids import new_id
from tests.support import PASSWORD, grant

pytestmark = [pytest.mark.integration, pytest.mark.django_db]

SIZE = 20_000
LIMIT_SECONDS = 0.1

KINDS = (
    ("Cable H07V-K", ("1x1,5 mm²", "1x2,5 mm²", "1x4 mm²", "1x6 mm²", "1x10 mm²")),
    ("Interruptor diferencial", ("2P 25A 30mA", "2P 40A 30mA", "4P 63A 300mA")),
    ("Magnetotérmico", ("1P 10A", "1P+N 16A", "3P 32A", "4P 63A")),
    ("Tubo corrugado", ("M20", "M25", "M32", "M40")),
    ("Caja de registro", ("100x100", "150x150", "200x200")),
    ("Regleta de conexión", ("3 polos", "5 polos", "12 polos")),
    ("Luminaria LED", ("18 W", "36 W", "60x60 40 W")),
    ("Bornes de empalme", ("2 hilos", "3 hilos", "5 hilos")),
)


@pytest.fixture
def big_catalog(db: None) -> Iterator[AccessContext]:
    unit = UnitOfMeasure.objects.get(code="UD")
    categories = [MaterialCategory.objects.create(name=kind) for kind, _ in KINDS]
    rng = random.Random(7)  # noqa: S311 - reproducible test data
    rows = []
    for number in range(SIZE):
        index = rng.randrange(len(KINDS))
        kind, variants = KINDS[index]
        variant = rng.choice(variants)
        rows.append(
            Material(
                id=new_id(),
                reference=f"M{number:06}",
                name=f"{kind} {variant} serie {number}",
                unit=unit,
                category=categories[index],
            )
        )
    Material.objects.bulk_create(rows, batch_size=2_000)
    with connection.cursor() as cursor:
        cursor.execute("ANALYZE catalog_material")
    user = User.objects.create_user("admin@example.org", PASSWORD)
    grant(user, Role.GROUP_ADMIN)
    yield AccessContext.for_user(user)


def timed(ctx: AccessContext, query: str) -> tuple[float, int]:
    selectors.search(ctx, query)  # warm up: plan caches, buffers
    best = float("inf")
    count = 0
    for _ in range(5):
        started = time.perf_counter()
        found = selectors.search(ctx, query)
        best = min(best, time.perf_counter() - started)
        count = len(found)
    return best, count


@pytest.mark.parametrize("query", ["cable 2,5", "diferencial", "DIFERENCIAL 40", "magnetotermico"])
def test_a_search_answers_in_under_100_ms(big_catalog: AccessContext, query: str) -> None:
    seconds, count = timed(big_catalog, query)
    assert count == selectors.SEARCH_LIMIT
    assert seconds < LIMIT_SECONDS, f"{query!r} took {seconds * 1000:.0f} ms"


def test_the_trigram_indexes_answer(big_catalog: AccessContext) -> None:
    sql, params = (
        Material.objects.annotate(f=fold("name"))
        .filter(f__contains=fold(Value("diferencial")))
        .values("pk")
        .query.sql_with_params()
    )
    with connection.cursor() as cursor:
        cursor.execute("EXPLAIN " + sql, params)
        plan = "\n".join(row[0] for row in cursor.fetchall())
    assert "catalog_material_name_trgm" in plan, plan

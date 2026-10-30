"""Composite foreign-key migration helpers (apps.core.migration_ops, plan §4.6)."""

from collections.abc import Iterator
from typing import Any

import pytest
from django.db import IntegrityError, connection, transaction

from apps.core.migration_ops import AddCompositeForeignKey, AddCompositeUnique

pytestmark = pytest.mark.django_db


def run(statements: Any) -> None:
    with connection.cursor() as cursor:
        for statement in [statements] if isinstance(statements, str) else statements:
            cursor.execute(statement)


@pytest.fixture
def tables() -> Iterator[None]:
    """A parent ``material``-like table and a child ``line``-like table."""
    run(
        [
            "CREATE TABLE mo_parent (id integer PRIMARY KEY, unit text NOT NULL)",
            "CREATE TABLE mo_child (id integer PRIMARY KEY, parent_id integer, unit text)",
            "INSERT INTO mo_parent VALUES (1, 'M'), (2, 'KG')",
        ]
    )
    yield


UNIQUE = AddCompositeUnique(name="mo_parent_id_unit", table="mo_parent", columns=("id", "unit"))
FK = AddCompositeForeignKey(
    name="mo_child_parent_unit_fk",
    table="mo_child",
    columns=("parent_id", "unit"),
    ref_table="mo_parent",
    ref_columns=("id", "unit"),
)


def test_child_must_match_the_parent_pair(tables: None) -> None:
    run(UNIQUE.sql)
    run(FK.sql)
    run("INSERT INTO mo_child VALUES (10, 1, 'M')")
    with pytest.raises(IntegrityError), transaction.atomic():
        run("INSERT INTO mo_child VALUES (11, 1, 'KG')")


def test_a_null_column_skips_the_check(tables: None) -> None:
    run(UNIQUE.sql)
    run(FK.sql)
    run("INSERT INTO mo_child VALUES (12, NULL, 'free text unit')")


def test_a_referenced_parent_key_cannot_change(tables: None) -> None:
    run(UNIQUE.sql)
    run(FK.sql)
    run("INSERT INTO mo_child VALUES (13, 2, 'KG')")
    with pytest.raises(IntegrityError), transaction.atomic():
        run("UPDATE mo_parent SET unit = 'L' WHERE id = 2")
    run("UPDATE mo_parent SET unit = 'L' WHERE id = 1")  # unreferenced: allowed


def test_the_constraint_is_immediate(tables: None) -> None:
    run(UNIQUE.sql)
    run(FK.sql)
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT condeferrable FROM pg_constraint WHERE conname = 'mo_child_parent_unit_fk'"
        )
        assert cursor.fetchone() == (False,)


def test_reverse_drops_everything(tables: None) -> None:
    run(UNIQUE.sql)
    run(FK.sql)
    run(FK.reverse_sql)
    run(UNIQUE.reverse_sql)
    run("INSERT INTO mo_child VALUES (14, 1, 'KG')")
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT count(*) FROM pg_indexes WHERE indexname = 'mo_child_parent_unit_fk_idx'"
        )
        assert cursor.fetchone() == (0,)


def test_index_is_optional() -> None:
    op = AddCompositeForeignKey(
        name="x_fk",
        table="x",
        columns=("a", "b"),
        ref_table="y",
        ref_columns=("c", "d"),
        index=False,
    )
    assert len(op.sql) == 1
    assert "CREATE INDEX" not in op.sql[0]


@pytest.mark.parametrize(
    "kwargs",
    [
        {"columns": ("a",), "ref_columns": ("c",)},
        {"columns": ("a", "a"), "ref_columns": ("c", "d")},
        {"columns": ("a", "b"), "ref_columns": ("c", "d", "e")},
        {"columns": ("a", "b; DROP TABLE x"), "ref_columns": ("c", "d")},
        {"columns": ("a", "b"), "ref_columns": ("c", "d"), "on_delete": "SET NULL"},
    ],
)
def test_invalid_definitions_are_refused(kwargs: dict[str, Any]) -> None:
    with pytest.raises(ValueError):
        AddCompositeForeignKey(name="x_fk", table="x", ref_table="y", **kwargs)


def test_identifiers_are_quoted() -> None:
    assert '"mo_parent"' in UNIQUE.sql
    with pytest.raises(ValueError):
        AddCompositeUnique(name="Bad-Name", table="t", columns=("a", "b"))

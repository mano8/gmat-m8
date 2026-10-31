"""Migration helpers for composite foreign keys (plan §4.6).

Cross-document rules C1-C3 and C9 are held by foreign keys over two columns, for example
``(material_id, unit_id) → catalog_material(id, unit_id)``. Django has no composite foreign
key field, so they are added with ``RunSQL``. The parent needs ``UNIQUE (id, <column>)``
first: declare it as a ``UniqueConstraint`` on the parent model, or add it here with
``AddCompositeUnique``. Both operations are reversible.

The constraints are ``NOT DEFERRABLE`` and ``ON UPDATE NO ACTION``: changing a referenced
parent key fails at once, and a concurrent first reference is serialized by the
``FOR KEY SHARE`` lock that the foreign-key check takes (C9).

Usage in a migration::

    operations = [
        AddCompositeForeignKey(
            name="mrl_material_unit_fk",
            table="material_requests_materialrequestline",
            columns=("material_id", "unit_id"),
            ref_table="catalog_material",
            ref_columns=("id", "unit_id"),
        ),
    ]
"""

import re
from collections.abc import Sequence

from django.db import migrations

IDENTIFIER = re.compile(r"[a-z_][a-z0-9_]{0,62}")
ON_DELETE = frozenset({"NO ACTION", "RESTRICT", "CASCADE"})


def _quote(identifier: str) -> str:
    if not IDENTIFIER.fullmatch(identifier):
        raise ValueError(f"Not a plain lowercase SQL identifier: {identifier!r}")
    return f'"{identifier}"'


def _column_list(columns: Sequence[str]) -> str:
    if len(columns) < 2 or len(set(columns)) != len(columns):
        raise ValueError("A composite key needs at least two distinct columns")
    return ", ".join(_quote(column) for column in columns)


def AddCompositeUnique(*, name: str, table: str, columns: Sequence[str]) -> migrations.RunSQL:
    """``UNIQUE (columns)`` on ``table``, the target of a composite foreign key."""
    forward = (
        f"ALTER TABLE {_quote(table)} ADD CONSTRAINT {_quote(name)}"
        f" UNIQUE ({_column_list(columns)});"
    )
    reverse = f"ALTER TABLE {_quote(table)} DROP CONSTRAINT {_quote(name)};"
    return migrations.RunSQL(forward, reverse)


def AddCompositeForeignKey(
    *,
    name: str,
    table: str,
    columns: Sequence[str],
    ref_table: str,
    ref_columns: Sequence[str],
    on_delete: str = "NO ACTION",
    index: bool = True,
) -> migrations.RunSQL:
    """``FOREIGN KEY (columns) REFERENCES ref_table (ref_columns)`` plus an index on the child.

    ``MATCH SIMPLE`` (the default) skips the check while any child column is NULL, which is
    what a nullable material on a free-text request line needs.
    """
    if len(columns) != len(ref_columns):
        raise ValueError("columns and ref_columns must have the same length")
    if on_delete not in ON_DELETE:
        raise ValueError(f"on_delete must be one of {sorted(ON_DELETE)}")
    child = _column_list(columns)
    forward = [
        f"ALTER TABLE {_quote(table)} ADD CONSTRAINT {_quote(name)}"
        f" FOREIGN KEY ({child}) REFERENCES {_quote(ref_table)} ({_column_list(ref_columns)})"
        f" ON UPDATE NO ACTION ON DELETE {on_delete} NOT DEFERRABLE;"
    ]
    reverse = [f"ALTER TABLE {_quote(table)} DROP CONSTRAINT {_quote(name)};"]
    if index:
        index_name = _quote(f"{name}_idx"[:63])
        forward.append(f"CREATE INDEX {index_name} ON {_quote(table)} ({child});")
        reverse.insert(0, f"DROP INDEX {index_name};")
    return migrations.RunSQL(tuple(forward), tuple(reverse))

"""The admin inventory is a contract (``docs/design/admin-inventory.md``, ADR-0030, M10.5).

Table 1 lists every model registered in the admin and where a person does that work in the
app. A model registered without a row, a row for a model that is no longer registered, a
URL name the matrix does not cover, or a row still waiting for its screen fails here, so a
new model cannot reach the admin unnoticed and nothing is left administered only there.
"""

import re
from pathlib import Path

from django.contrib import admin
from django.urls import NoReverseMatch, reverse

from tests.access.matrix import TABLE_METHODS
from tests.access.test_acceptance_matrix import ROUTES

INVENTORY = Path(__file__).resolve().parents[2] / "docs" / "design" / "admin-inventory.md"
CODE = re.compile(r"`([^`]+)`")


def _table_one() -> list[list[str]]:
    """The rows of section 1, each as its cells."""
    text = INVENTORY.read_text(encoding="utf-8")
    section = text.split("## 1. ", 1)[1].split("\n## ", 1)[0]
    rows = [line for line in section.splitlines() if line.startswith("| `")]
    assert rows, "table 1 of the inventory has no rows"
    return [[cell.strip() for cell in row.strip("|").split("|")] for row in rows]


def _label(row: list[str]) -> str:
    found = CODE.search(row[0])
    assert found, row
    return found[1]


def _url_names(row: list[str]) -> list[str]:
    return CODE.findall(row[3])


def test_every_registered_model_has_a_row_and_every_row_a_model() -> None:
    listed = {_label(row) for row in _table_one()}
    registered = {model._meta.label for model in admin.site._registry}
    assert registered - listed == set(), "registered in the admin, missing from the inventory"
    assert listed - registered == set(), "in the inventory, no longer registered"


def test_every_screen_named_is_in_the_matrix() -> None:
    """Each URL name exists and has its GET, HEAD and POST rows for every actor."""
    for row in _table_one():
        for name in _url_names(row):
            assert name in ROUTES, f"{_label(row)}: {name} has no row in ROUTES"
            for route in ROUTES[name]:
                assert set(route.methods) == set(TABLE_METHODS), name
            try:
                reverse(name)
            except NoReverseMatch as exc:
                # A URL with arguments: the name exists if only the arguments are missing.
                assert "with no arguments not found" in str(exc), name


def test_nothing_waits_for_a_screen() -> None:
    """Every row is a screen or internal: the admin is never the only place (ADR-0030)."""
    waiting = [_label(row) for row in _table_one() if "needs a screen" in row[2]]
    assert waiting == []

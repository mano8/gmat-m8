"""Spreadsheet-safe cells (plan M7.3)."""

from datetime import UTC, date, datetime
from decimal import Decimal

import pytest

from apps.exports.cells import cell, safe_text


@pytest.mark.parametrize("start", ["=", "+", "-", "@", "\t", "\r"])
def test_text_that_could_be_a_formula_is_quoted(start: str) -> None:
    assert cell(f'{start}HYPERLINK("http://evil","x")') == f'\'{start}HYPERLINK("http://evil","x")'


def test_ordinary_text_is_untouched() -> None:
    assert safe_text("Cable 2,5 =x") == "Cable 2,5 =x"
    assert cell("") == ""


def test_values_keep_their_type_or_a_plain_form() -> None:
    assert cell(None) == ""
    assert cell(7) == 7
    assert cell(Decimal("-1.500")) == "-1.500"
    assert cell(Decimal("1E+2")) == "100"
    assert cell(date(2026, 11, 2)) == "2026-11-02"
    assert cell(datetime(2026, 11, 2, 9, 30, 5, 123, tzinfo=UTC)) == "2026-11-02T09:30:05+00:00"

"""Spreadsheet-safe cells (plan M7.3).

A text cell that starts with ``=``, ``+``, ``-``, ``@``, a tab or a carriage return is read as a
formula by Excel and LibreOffice. Free text (notes, descriptions, names) reaches the exports from
people who are not the reader, so every string is prefixed with an apostrophe when it could start
a formula. Numbers, dates and booleans are written as such, never as text.
"""

from datetime import date, datetime
from decimal import Decimal
from typing import Any

FORMULA_STARTS = ("=", "+", "-", "@", "\t", "\r")


def safe_text(value: str) -> str:
    return f"'{value}" if value.startswith(FORMULA_STARTS) else value


def cell(value: Any) -> Any:
    """One value as the CSV writer should receive it."""
    if value is None:
        return ""
    if isinstance(value, str):
        return safe_text(value)
    if isinstance(value, Decimal):
        return f"{value:f}"
    if isinstance(value, datetime):
        return value.isoformat(timespec="seconds")
    if isinstance(value, date):
        return value.isoformat()
    return value

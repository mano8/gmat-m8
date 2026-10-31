"""Helpers for the import-command tests: write a file, run the command, read its report."""

from collections.abc import Sequence
from io import StringIO
from pathlib import Path

from django.core.management import call_command
from django.core.management.base import CommandError

CSV_HEADER_NOTE = "Rows are plain text; the first line is the header."


def write_csv(path: Path, lines: Sequence[str], *, encoding: str = "utf-8") -> Path:
    path.write_bytes(("\n".join(lines) + "\n").encode(encoding))
    return path


def write_xlsx(path: Path, rows: Sequence[Sequence[object]], *, sheet: str | None = None) -> Path:
    from openpyxl import Workbook

    workbook = Workbook()
    worksheet = workbook.active
    assert worksheet is not None
    if sheet is not None:
        worksheet.title = sheet
    for row in rows:
        worksheet.append(list(row))
    workbook.save(path)
    return path


def run_import(command: str, path: Path, operator: str, *args: str) -> str:
    """The command's output; a refusal raises ``CommandError`` carrying the output."""
    out = StringIO()
    try:
        call_command(command, str(path), "--operator", operator, *args, stdout=out)
    except CommandError as exc:
        exc.output = out.getvalue()  # type: ignore[attr-defined]
        raise
    return out.getvalue()

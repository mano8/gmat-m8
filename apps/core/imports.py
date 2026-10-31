"""Base class of the import commands (plan M3.3): ``import_catalog``, ``import_suppliers``,
``import_projects``.

Each command is an ``OperatorCommand`` (``--operator <email>``, the matching capability) and
reads a CSV or XLSX file (``apps.core.tabular``):

1. the file is read and its columns mapped by name; an unreadable file or a missing
   required column stops here;
2. every row is checked without touching the database (``parse``), and every error is
   collected with its line;
3. in one transaction (``--dry-run`` rolls it back at the end) each row is applied through
   the same services as the app (``apply``), so their invariants, audit and authorization
   hold; each row runs in a savepoint so one bad row does not hide the others;
4. any error, from step 2 or 3, rolls the whole file back: **nothing is written** and the
   report lists every error by line;
5. otherwise one audit event records the import (``via=cli:<command>``, the file's name and
   hash, the counts).

An import is idempotent: rows are matched by their natural key, services write nothing
when nothing changed, and a second run reports every row as unchanged.
"""

import hashlib
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, ClassVar

from django.core.exceptions import ValidationError
from django.core.management.base import CommandError, CommandParser
from django.db import transaction

from apps.access.context import AccessContext
from apps.access.exceptions import AccessDenied, NotInScope
from apps.access.principals import OperatorPrincipal
from apps.accounts.models import User
from apps.audit.recorder import record
from apps.core.commands import OperatorCommand
from apps.core.locking import RetryExhausted, run_in_transaction
from apps.core.tabular import Column, Row, Table, TableError, read_table

MAX_REPORTED_ERRORS = 200

CREATED, UPDATED, UNCHANGED = "created", "updated", "unchanged"
YES = frozenset({"1", "y", "yes", "true", "t", "x", "s", "si", "sí", "oui", "o", "verdadero"})
NO = frozenset({"0", "n", "no", "false", "f", "falso", "non"})


class RowRefused(Exception):
    """A row the command rejects; ``column`` names the cell when one is to blame."""

    def __init__(self, message: str, column: str | None = None) -> None:
        super().__init__(message)
        self.column = column


@dataclass
class RowError:
    line: int
    column: str | None
    message: str


@dataclass
class Report:
    """What an import found: the column mapping, the errors by line and the counts."""

    command: str
    table: Table
    errors: list[RowError] = field(default_factory=list)
    counts: Counter[str] = field(default_factory=Counter)
    dry_run: bool = False
    written: bool = False

    def error(self, line: int, message: str, column: str | None = None) -> None:
        self.errors.append(RowError(line, column, message))

    def text(self, columns: tuple[Column, ...]) -> str:
        table = self.table
        out = [f"{self.command}: {table.source} ({table.format}, {len(table.rows)} rows)"]
        if table.encoding and table.encoding != "utf-8-sig":
            out.append(f"  read as {table.encoding}")
        out.append("Columns:")
        for column in columns:
            header = table.mapped.get(column.key)
            note = (
                f'"{header}"' if header else ("MISSING" if column.required else "not in the file")
            )
            out.append(f"  {column.key:<18} <- {note}")
        if table.ignored:
            out.append("Ignored columns: " + ", ".join(f'"{name}"' for name in table.ignored))
        if self.errors:
            out.append("Errors:")
            for error in self.errors[:MAX_REPORTED_ERRORS]:
                where = f"{error.column}: " if error.column else ""
                out.append(f"  line {error.line}: {where}{error.message}")
            if len(self.errors) > MAX_REPORTED_ERRORS:
                out.append(f"  ... and {len(self.errors) - MAX_REPORTED_ERRORS} more")
            out.append(f"{len(self.errors)} error(s): nothing was written.")
        else:
            counts = ", ".join(f"{self.counts[key]} {key}" for key in self.counts) or "no rows"
            suffix = "dry run, nothing was written" if self.dry_run else "committed"
            out.append(f"Result: {counts} ({suffix}).")
        return "\n".join(out)


def parse_bool(value: str, column: str) -> bool:
    lowered = value.strip().casefold()
    if lowered in YES:
        return True
    if lowered in NO:
        return False
    raise RowRefused(f"expected yes or no, got {value!r}", column)


def require(row: Row, key: str) -> str:
    value = row.get(key)
    if not value:
        raise RowRefused("is required", key)
    return value


class ImportCommand(OperatorCommand):
    """Subclasses declare ``columns``, ``audit_action`` and implement ``parse`` / ``apply``."""

    abstract = True

    columns: ClassVar[tuple[Column, ...]]
    audit_action: ClassVar[str]

    def add_arguments(self, parser: CommandParser) -> None:
        super().add_arguments(parser)
        parser.add_argument("file", help="A .csv or .xlsx file (see docs/import-templates/)")
        parser.add_argument(
            "--dry-run", action="store_true", help="Validate and report; write nothing"
        )
        parser.add_argument("--sheet", help="XLSX sheet name (default: the first sheet)")
        parser.add_argument("--delimiter", help="CSV delimiter (default: taken from the header)")

    # --- hooks ---------------------------------------------------------------------

    def parse(self, row: Row) -> Any:
        """Check one row without the database; return what ``apply`` needs.

        Raise ``RowRefused`` for a row that cannot be right whatever the data holds.
        """
        raise NotImplementedError

    def begin(self, principal: OperatorPrincipal, ctx: AccessContext) -> None:
        """Called once per transaction attempt, before the first row (load lookups here)."""

    def natural_key(self, parsed: Any) -> str | None:
        """The key rows are matched by; two rows of a file with the same key are an error."""
        return None

    def apply(self, principal: OperatorPrincipal, parsed: Any) -> str:
        """Write one parsed row through the services; return ``created``, ``updated`` or
        ``unchanged``. A ``ValidationError`` or ``RowRefused`` reports the row's line."""
        raise NotImplementedError

    def summary(self) -> dict[str, Any]:
        """Extra counts for the audit event (for example categories created)."""
        return {}

    # --- the run -------------------------------------------------------------------

    def handle_operator(
        self, principal: OperatorPrincipal, *args: Any, **options: Any
    ) -> str | None:
        path = Path(options["file"])
        try:
            table = read_table(
                path, self.columns, sheet=options["sheet"], delimiter=options["delimiter"]
            )
        except TableError as exc:
            raise CommandError(f"{self.command_name}: {exc}") from exc
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        ctx = AccessContext.for_user(User.objects.get(pk=principal.user_id))
        dry_run = bool(options["dry_run"])

        parsed_rows: list[tuple[Row, Any]] = []
        pre = Report(self.command_name, table, dry_run=dry_run)
        seen: dict[str, int] = {}
        for row in table.rows:
            try:
                parsed = self.parse(row)
                key = self.natural_key(parsed)
                if key is not None and key in seen:
                    raise RowRefused(f"is the same as line {seen[key]}")
                if key is not None:
                    seen[key] = row.line
                parsed_rows.append((row, parsed))
            except RowRefused as exc:
                pre.error(row.line, str(exc), exc.column)
        # A file with errors is still tried against the database in a transaction that is
        # rolled back, so the report lists what the data refuses as well as what the rows say.
        rehearsal = dry_run or bool(pre.errors)
        try:
            report = run_in_transaction(
                lambda: self._apply_all(principal, ctx, table, parsed_rows, digest, rehearsal)
            )
        except RetryExhausted as exc:
            raise CommandError(f"{self.command_name}: the database stayed busy; retry") from exc
        report.dry_run = dry_run
        report.errors = sorted([*pre.errors, *report.errors], key=lambda error: error.line)
        self.stdout.write(report.text(self.columns))
        if report.errors:
            raise CommandError(f"{self.command_name}: the file has errors; nothing was written")
        return None

    def _apply_all(
        self,
        principal: OperatorPrincipal,
        ctx: AccessContext,
        table: Table,
        parsed_rows: list[tuple[Row, Any]],
        digest: str,
        dry_run: bool,
    ) -> Report:
        report = Report(self.command_name, table, dry_run=dry_run)
        self.begin(principal, ctx)
        for row, parsed in parsed_rows:
            try:
                with transaction.atomic():
                    outcome = self.apply(principal, parsed)
            except RowRefused as exc:
                report.error(row.line, str(exc), exc.column)
            except ValidationError as exc:
                self._validation_errors(report, row.line, exc)
            except AccessDenied, NotInScope:
                report.error(row.line, "the operator may not change this record")
            else:
                report.counts[outcome] += 1
        if report.errors or dry_run:
            transaction.set_rollback(True)
        else:
            record(
                principal.audit_identity,
                self.audit_action,
                data={
                    "file": table.source,
                    "sha256": digest,
                    "rows": len(table.rows),
                    **dict(report.counts),
                    **self.summary(),
                },
            )
            report.written = True
        return report

    @staticmethod
    def _validation_errors(report: Report, line: int, exc: ValidationError) -> None:
        if hasattr(exc, "error_dict"):
            for column, errors in exc.error_dict.items():
                for error in errors:
                    report.error(
                        line, " ".join(error.messages), None if column == "__all__" else column
                    )
        else:
            report.error(line, " ".join(exc.messages))

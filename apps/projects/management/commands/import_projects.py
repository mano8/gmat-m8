"""``manage import_projects <file> --operator <email> [--dry-run]`` (plan M3.3).

Upserts projects by ``code`` from a CSV or XLSX file, through the project services (a new
project gets its worksite location); see ``apps.core.imports`` for the run and
``docs/import-templates/`` for the columns. Branches are written ``COMPANY-BRANCH`` (for
example ``A-MAD``) and must lie in the operator's ``project.manage`` scope. A blank
``procurement_branch`` or ``status`` cell leaves a project's value alone (new projects take
the defaults of plan §3.3); other blank cells clear the field.
"""

import re
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any, ClassVar
from uuid import UUID

from apps.access.context import AccessContext
from apps.access.principals import OperatorPrincipal
from apps.access.roles import Capability
from apps.core.imports import CREATED, UNCHANGED, UPDATED, ImportCommand, RowRefused, require
from apps.core.tabular import Column, Row, normalize_header
from apps.organizations import selectors as org_selectors
from apps.organizations.models import CODE_PATTERN, Branch
from apps.projects import selectors, services
from apps.projects.models import Project

COLUMNS = (
    Column("code", ("codigo", "project", "proyecto", "obra", "chantier"), required=True),
    Column("name", ("nombre", "nom"), required=True),
    Column("branch", ("sucursal", "delegacion", "succursale"), required=True),
    Column("procurement_branch", ("compras", "sucursal_de_compras", "buying_branch")),
    Column("client_name", ("client", "cliente")),
    Column("address", ("direccion", "adresse")),
    Column("status", ("estado", "statut")),
    Column("start_date", ("inicio", "fecha_inicio", "start", "debut")),
    Column("end_date", ("fin", "fecha_fin", "end")),
)
DATE_FORMATS = ("%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y")
STATUSES = {normalize_header(s.value): s.value for s in Project.Status} | {
    normalize_header(str(s.label)): s.value for s in Project.Status
}


def parse_date(value: str, column: str) -> date | None:
    if not value:
        return None
    for fmt in DATE_FORMATS:
        try:
            return datetime.strptime(value, fmt).date()
        except ValueError:
            continue
    raise RowRefused(f"{value!r} is not a date (use YYYY-MM-DD)", column)


@dataclass(frozen=True)
class ProjectRow:
    code: str
    name: str
    branch: str
    procurement_branch: str
    status: str
    values: dict[str, Any]  # client_name, address, start_date, end_date as the file has them


class Command(ImportCommand):
    help = "Import or update projects from a CSV or XLSX file."
    required_capability: ClassVar[str] = Capability.PROJECT_MANAGE
    columns = COLUMNS
    audit_action = "project.imported"

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.ctx: AccessContext
        self.branches: dict[str, Branch] = {}

    def natural_key(self, parsed: ProjectRow) -> str:
        return parsed.code

    def parse(self, row: Row) -> ProjectRow:
        code = require(row, "code").upper()
        if not re.fullmatch(CODE_PATTERN, code) or len(code) > 30:
            raise RowRefused(
                f"{code!r}: use upper-case letters, digits, '-' and '_' (30 characters at most)",
                "code",
            )
        name = require(row, "name")
        if len(name) > 200:
            raise RowRefused("is longer than 200 characters", "name")
        status = ""
        if row.get("status"):
            status = STATUSES.get(normalize_header(row.get("status")), "")
            if not status:
                choices = ", ".join(sorted(set(STATUSES.values())))
                raise RowRefused(f"{row.get('status')!r} is not a status ({choices})", "status")
        values: dict[str, Any] = {}
        for key in ("client_name", "address"):
            if key in row.values:
                values[key] = row.get(key)
        for key in ("start_date", "end_date"):
            if key in row.values:
                values[key] = parse_date(row.get(key), key)
        start, end = values.get("start_date"), values.get("end_date")
        if start and end and end < start:
            raise RowRefused("is before the start date", "end_date")
        return ProjectRow(
            code,
            name,
            require(row, "branch").upper(),
            row.get("procurement_branch").upper(),
            status,
            values,
        )

    def begin(self, principal: OperatorPrincipal, ctx: AccessContext) -> None:
        self.ctx = ctx
        self.branches = {
            f"{branch.company.code}-{branch.code}".upper(): branch
            for branch in org_selectors.branches(ctx, Capability.PROJECT_MANAGE)
        }

    def _branch(self, spec: str, column: str) -> UUID:
        branch = self.branches.get(spec)
        if branch is None:
            raise RowRefused(
                f"unknown branch {spec!r} (write COMPANY-BRANCH, for example A-MAD; "
                "it must be active and in the operator's scope)",
                column,
            )
        return branch.pk

    def apply(self, principal: OperatorPrincipal, parsed: ProjectRow) -> str:
        branch_id = self._branch(parsed.branch, "branch")
        procurement_id = (
            self._branch(parsed.procurement_branch, "procurement_branch")
            if parsed.procurement_branch
            else None
        )
        existing = selectors.project_by_code(self.ctx, parsed.code)
        fields = dict(parsed.values, name=parsed.name)
        if existing is None:
            project = services.create_project(
                principal,
                code=parsed.code,
                branch_id=branch_id,
                procurement_branch_id=procurement_id,
                **fields,
            )
            if parsed.status and parsed.status != project.status:
                services.update_project(principal, project.pk, status=parsed.status)
            return CREATED
        fields["branch_id"] = branch_id
        if procurement_id is not None:
            fields["procurement_branch_id"] = procurement_id
        if parsed.status:
            fields["status"] = parsed.status
        changes = {key: value for key, value in fields.items() if getattr(existing, key) != value}
        if not changes:
            return UNCHANGED
        services.update_project(principal, existing.pk, **changes)
        return UPDATED

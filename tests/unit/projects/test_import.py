"""``import_projects`` (plan M3.3)."""

from pathlib import Path
from typing import Any

import pytest
from django.core.management.base import CommandError

from apps.audit.models import AuditEvent
from apps.locations.models import Location
from apps.projects.models import Project
from tests.imports import run_import, write_csv, write_xlsx
from tests.org import Org

pytestmark = pytest.mark.django_db

HEADER = "code,name,branch,procurement_branch,client_name,address,status,start_date,end_date"
GOOD = [
    HEADER,
    "NEW-1,Nave industrial,A-MAD,,Cliente SA,Calle 1,,2026-11-01,2027-03-31",
    "NEW-2,Colegio,B-MAD,A-SEV,,,on hold,01/12/2026,",
    "new-3,Oficinas,A-SEV,,,,closed,,",
]


@pytest.fixture
def operator(org: Org, people: Any) -> str:
    return str(people.group_admin.email)


class TestImport:
    def test_creates_projects_with_their_worksites_and_audits_once(
        self, tmp_path: Path, operator: str, org: Org
    ) -> None:
        out = run_import("import_projects", write_csv(tmp_path / "p.csv", GOOD), operator)
        assert "3 created" in out
        one, two, three = (Project.objects.get(code=f"NEW-{n}") for n in (1, 2, 3))
        assert (one.branch, one.procurement_branch) == (org.a_mad, org.a_mad)
        assert (one.client_name, str(one.start_date), str(one.end_date)) == (
            "Cliente SA",
            "2026-11-01",
            "2027-03-31",
        )
        assert (two.branch, two.procurement_branch, two.status) == (
            org.b_mad,
            org.a_sev,
            Project.Status.ON_HOLD,
        )
        assert str(two.start_date) == "2026-12-01"
        assert three.status == Project.Status.CLOSED
        assert Location.objects.filter(project__code__startswith="NEW-").count() == 3
        (event,) = AuditEvent.objects.filter(action="project.imported")
        assert event.via == "cli:import_projects" and event.data["created"] == 3

    def test_the_procurement_branch_defaults_to_the_managing_branch(
        self, tmp_path: Path, operator: str, org: Org
    ) -> None:
        path = write_csv(tmp_path / "p.csv", ["code,name,branch", "NEW-9,Obra,B-MAD"])
        run_import("import_projects", path, operator)
        assert Project.objects.get(code="NEW-9").procurement_branch == org.a_mad

    def test_importing_the_same_file_twice_changes_nothing(
        self, tmp_path: Path, operator: str, org: Org
    ) -> None:
        path = write_csv(tmp_path / "p.csv", GOOD)
        run_import("import_projects", path, operator)
        stamps = list(Project.objects.order_by("code").values_list("pk", "updated_at"))
        audit = AuditEvent.objects.exclude(action="project.imported").count()
        out = run_import("import_projects", path, operator)
        assert "3 unchanged" in out
        assert list(Project.objects.order_by("code").values_list("pk", "updated_at")) == stamps
        assert AuditEvent.objects.exclude(action="project.imported").count() == audit

    def test_updates(self, tmp_path: Path, operator: str, org: Org) -> None:
        run_import("import_projects", write_csv(tmp_path / "p.csv", GOOD), operator)
        changed = [
            HEADER,
            "NEW-1,Nave renombrada,A-MAD,,Cliente SA,Calle 1,ACTIVE,2026-11-01,2027-03-31",
            "NEW-2,Colegio,B-MAD,,,,,,",  # blank procurement and status: left alone; dates cleared
        ]
        out = run_import("import_projects", write_csv(tmp_path / "c.csv", changed), operator)
        assert "2 updated" in out
        assert Project.objects.get(code="NEW-1").name == "Nave renombrada"
        two = Project.objects.get(code="NEW-2")
        assert (two.procurement_branch, two.status, two.start_date) == (
            org.a_sev,
            Project.Status.ON_HOLD,
            None,
        )

    def test_xlsx_with_dates(self, tmp_path: Path, operator: str, org: Org) -> None:
        from datetime import date

        path = write_xlsx(
            tmp_path / "p.xlsx",
            [
                ["Código", "Nombre", "Sucursal", "Inicio"],
                ["X-1", "Obra", "A-MAD", date(2026, 5, 1)],
            ],
        )
        run_import("import_projects", path, operator)
        assert str(Project.objects.get(code="X-1").start_date) == "2026-05-01"


class TestErrors:
    def test_a_file_with_errors_writes_nothing(
        self, tmp_path: Path, operator: str, org: Org
    ) -> None:
        lines = [
            HEADER,
            "OK-1,Fine,A-MAD,,,,,,",
            "bad code!,Thing,A-MAD,,,,,,",  # line 3
            "OK-3,Thing,A-MAD,,,,,2026-13-45,",  # line 4
            "OK-4,Thing,A-MAD,,,,,2026-05-02,2026-05-01",  # line 5
            "OK-5,Thing,A-MAD,,,,paused,,",  # line 6
            "OK-6,Thing,Z-NOPE,,,,,,",  # line 7: only the database can tell
            "OK-1,Again,A-MAD,,,,,,",  # line 8: the code of line 2
        ]
        with pytest.raises(CommandError, match="nothing was written") as caught:
            run_import("import_projects", write_csv(tmp_path / "e.csv", lines), operator)
        report = caught.value.output  # type: ignore[attr-defined]
        for expected in (
            "line 3: code:",
            "line 4: start_date: '2026-13-45' is not a date",
            "line 5: end_date: is before the start date",
            "line 6: status: 'paused' is not a status",
            "line 7: branch: unknown branch 'Z-NOPE'",
            "line 8: is the same as line 2",
        ):
            assert expected in report, report
        assert not Project.objects.filter(code__startswith="OK-").exists()
        assert not AuditEvent.objects.filter(action="project.imported").exists()

    def test_dry_run(self, tmp_path: Path, operator: str, org: Org) -> None:
        out = run_import(
            "import_projects", write_csv(tmp_path / "p.csv", GOOD), operator, "--dry-run"
        )
        assert "3 created" in out and "dry run" in out
        assert not Project.objects.filter(code__startswith="NEW-").exists()


class TestOperator:
    def test_a_material_manager_imports_within_their_branches(
        self, tmp_path: Path, org: Org, people: Any
    ) -> None:
        lines = [HEADER, "MM-1,In scope,A-MAD,,,,,,", "MM-2,B-MAD is managed,B-MAD,,,,,,"]
        run_import("import_projects", write_csv(tmp_path / "p.csv", lines), people.mm_mad.email)
        assert Project.objects.filter(code__in=["MM-1", "MM-2"]).count() == 2
        outside = [HEADER, "MM-3,Not theirs,A-SEV,,,,,,"]
        with pytest.raises(CommandError) as caught:
            run_import(
                "import_projects", write_csv(tmp_path / "o.csv", outside), people.mm_mad.email
            )
        assert "line 2: branch: unknown branch 'A-SEV'" in caught.value.output  # type: ignore[attr-defined]
        assert not Project.objects.filter(code="MM-3").exists()

    def test_updating_a_project_outside_the_scope_is_refused(
        self, tmp_path: Path, org: Org, people: Any
    ) -> None:
        lines = [HEADER, f"{org.sev_works.code},Renamed,A-MAD,,,,,,"]
        with pytest.raises(CommandError) as caught:
            run_import("import_projects", write_csv(tmp_path / "p.csv", lines), people.mm_mad.email)
        assert "line 2" in caught.value.output  # type: ignore[attr-defined]
        org.sev_works.refresh_from_db()
        assert org.sev_works.name == org.sev_works.code  # untouched

    def test_operators_without_the_capability_are_refused(
        self, tmp_path: Path, org: Org, people: Any
    ) -> None:
        for who in ("wo_mad", "tech_mad", "site_manager", "partner", "revoked", "nobody"):
            with pytest.raises(CommandError, match=r"does not hold project\.manage|active user"):
                run_import(
                    "import_projects",
                    write_csv(tmp_path / "p.csv", GOOD),
                    getattr(people, who).email,
                )
        assert not Project.objects.filter(code__startswith="NEW-").exists()

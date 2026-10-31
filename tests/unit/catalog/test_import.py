"""``import_catalog`` (plan M3.3): the same services, one transaction per file."""

from pathlib import Path
from typing import Any

import pytest
from django.core.management.base import CommandError

from apps.audit.models import AuditEvent
from apps.catalog.models import Material, MaterialCategory
from tests.imports import run_import, write_csv, write_xlsx
from tests.org import Org

pytestmark = pytest.mark.django_db

HEADER = "reference,name,unit,category,description,manufacturer,manufacturer_ref,ean,stock,active"
GOOD = [
    HEADER,
    "CAB-25,Cable H07V-K 1x2.5,M,Cables > Unipolares,Rigid,Prysmian,P-25,8412345678905,yes,",
    "DIF-40,Diferencial 40A,UD,Protección,,Schneider,,,,",
    "MAG-16,Magnetotérmico 16A,UD,Protección,,,,,no,no",
]


@pytest.fixture
def operator(org: Org, people: Any) -> str:
    return str(people.group_admin.email)


def events(action: str) -> list[AuditEvent]:
    return list(AuditEvent.objects.filter(action=action))


class TestImport:
    def test_creates_materials_and_categories_and_audits_once(
        self, tmp_path: Path, operator: str
    ) -> None:
        out = run_import("import_catalog", write_csv(tmp_path / "c.csv", GOOD), operator)
        assert "3 created" in out and "committed" in out
        cable = Material.objects.get(reference="CAB-25")
        assert (cable.unit.code, cable.manufacturer, cable.ean) == (
            "M",
            "Prysmian",
            "8412345678905",
        )
        assert cable.is_stock_item and cable.is_active
        assert str(cable.category) == "Cables / Unipolares"
        assert not Material.objects.get(reference="MAG-16").is_active
        assert not Material.objects.get(reference="DIF-40").is_stock_item
        assert MaterialCategory.objects.count() == 3  # Cables, Unipolares, Protección
        (event,) = events("catalog.imported")
        assert event.via == "cli:import_catalog"
        assert event.data["created"] == 3 and event.data["categories_created"] == 3
        assert event.data["file"] == "c.csv" and len(event.data["sha256"]) == 64

    def test_importing_the_same_file_twice_changes_nothing(
        self, tmp_path: Path, operator: str
    ) -> None:
        path = write_csv(tmp_path / "c.csv", GOOD)
        run_import("import_catalog", path, operator)
        before = {
            "materials": list(
                Material.objects.order_by("reference").values_list("pk", "updated_at")
            ),
            "categories": MaterialCategory.objects.count(),
            "audit": AuditEvent.objects.exclude(action="catalog.imported").count(),
        }
        out = run_import("import_catalog", path, operator)
        assert "3 unchanged" in out
        assert (
            list(Material.objects.order_by("reference").values_list("pk", "updated_at"))
            == (before["materials"])
        )
        assert MaterialCategory.objects.count() == before["categories"]
        assert AuditEvent.objects.exclude(action="catalog.imported").count() == before["audit"]

    def test_updates_what_changed(self, tmp_path: Path, operator: str) -> None:
        run_import("import_catalog", write_csv(tmp_path / "c.csv", GOOD), operator)
        changed = [
            HEADER,
            "CAB-25,Cable H07V-K 1x2.5 azul,M,Cables,Rigid,Prysmian,P-25,8412345678905,yes,",
            "dif-40,Diferencial 40A,UD,Protección,,Schneider,,,,",  # same, any case
            "MAG-16,Magnetotérmico 16A,UD,Protección,,,,,,yes",  # reactivated
        ]
        out = run_import("import_catalog", write_csv(tmp_path / "d.csv", changed), operator)
        assert "2 updated" in out and "1 unchanged" in out
        cable = Material.objects.get(reference="CAB-25")
        assert cable.name.endswith("azul") and str(cable.category) == "Cables"
        assert Material.objects.get(reference="MAG-16").is_active
        assert Material.objects.count() == 3

    def test_a_blank_text_cell_clears_and_a_blank_yes_no_cell_does_not(
        self, tmp_path: Path, operator: str
    ) -> None:
        run_import("import_catalog", write_csv(tmp_path / "c.csv", GOOD), operator)
        blank = [HEADER, "CAB-25,Cable H07V-K 1x2.5,M,,,,,,,"]
        run_import("import_catalog", write_csv(tmp_path / "b.csv", blank), operator)
        cable = Material.objects.get(reference="CAB-25")
        assert (cable.category, cable.manufacturer, cable.ean) == (None, "", None)
        assert cable.is_stock_item and cable.is_active  # untouched

    def test_columns_not_in_the_file_are_left_alone(self, tmp_path: Path, operator: str) -> None:
        run_import("import_catalog", write_csv(tmp_path / "c.csv", GOOD), operator)
        narrow = write_csv(tmp_path / "n.csv", ["reference,name,unit", "CAB-25,Renamed,M"])
        out = run_import("import_catalog", narrow, operator)
        assert "1 updated" in out and "category" in out and "not in the file" in out
        cable = Material.objects.get(reference="CAB-25")
        assert (cable.name, cable.manufacturer, str(cable.category)) == (
            "Renamed",
            "Prysmian",
            "Cables / Unipolares",
        )

    def test_spanish_headers_semicolons_and_windows_1252(
        self, tmp_path: Path, operator: str
    ) -> None:
        path = write_csv(
            tmp_path / "es.csv",
            ["Referencia;Nombre;Unidad;Categoría", "TUB-20;Tubo rígido Ø20;UD;Tubos"],
            encoding="cp1252",
        )
        out = run_import("import_catalog", path, operator)
        assert "Windows" not in out and "read as cp1252" in out
        assert Material.objects.get(reference="TUB-20").name == "Tubo rígido Ø20"

    def test_xlsx(self, tmp_path: Path, operator: str) -> None:
        path = write_xlsx(
            tmp_path / "c.xlsx",
            [
                ["Reference", "Name", "Unit", "EAN", "Stock"],
                ["X-1", "Thing", "UD", 8412345678905, True],
                ["X-2", "Other", "ud", None, None],
            ],
        )
        run_import("import_catalog", path, operator)
        assert Material.objects.get(reference="X-1").ean == "8412345678905"
        assert Material.objects.get(reference="X-1").is_stock_item
        assert Material.objects.get(reference="X-2").unit.code == "UD"


class TestErrors:
    def test_a_file_with_errors_writes_nothing_and_reports_every_line(
        self, tmp_path: Path, operator: str
    ) -> None:
        lines = [
            HEADER,
            "OK-1,Fine,UD,Cat,,,,,,",
            ",No reference,UD,,,,,,,",  # line 3
            "BAD-EAN,Thing,UD,,,,,123,,",  # line 4
            "OK-1,Duplicate,UD,,,,,,,",  # line 5: same as line 2
            "BAD-YN,Thing,UD,,,,,,maybe,",  # line 6
            "BAD-CAT,Thing,UD,A > B > C,,,,,,",  # line 7
            "BAD-UNIT,Thing,PARSEC,,,,,,,",  # line 8: only the database can tell
        ]
        with pytest.raises(CommandError, match="nothing was written") as caught:
            run_import("import_catalog", write_csv(tmp_path / "e.csv", lines), operator)
        report = caught.value.output  # type: ignore[attr-defined]
        for expected in (
            "line 3: reference: is required",
            "line 4: ean:",
            "line 5: is the same as line 2",
            "line 6: is_stock_item: expected yes or no",
            "line 7: category: has more than 2 levels",
            "line 8: unit: unknown unit 'PARSEC'",  # found by the rehearsal in the database
        ):
            assert expected in report, report
        assert not Material.objects.exists() and not MaterialCategory.objects.exists()
        assert not events("catalog.imported")

    def test_an_error_found_while_writing_rolls_back_the_earlier_rows(
        self, tmp_path: Path, operator: str
    ) -> None:
        lines = [
            HEADER,
            "OK-1,Fine,UD,Cables,,,,,,",
            "OK-2,Fine too,UD,Cables > Sub,,,,,,",
            "BAD,Thing,PARSEC,,,,,,,",
        ]
        with pytest.raises(CommandError) as caught:
            run_import("import_catalog", write_csv(tmp_path / "e.csv", lines), operator)
        report = caught.value.output  # type: ignore[attr-defined]
        assert "line 4: unit: unknown unit 'PARSEC'" in report and "UD" in report
        assert not Material.objects.exists() and not MaterialCategory.objects.exists()
        assert not AuditEvent.objects.filter(action__startswith="material.").exists()

    def test_dry_run_reports_and_writes_nothing(self, tmp_path: Path, operator: str) -> None:
        out = run_import(
            "import_catalog", write_csv(tmp_path / "c.csv", GOOD), operator, "--dry-run"
        )
        assert "3 created" in out and "dry run" in out
        assert not Material.objects.exists() and not MaterialCategory.objects.exists()
        assert not events("catalog.imported")

    def test_dry_run_still_reports_errors(self, tmp_path: Path, operator: str) -> None:
        lines = [HEADER, "BAD,Thing,PARSEC,,,,,,,"]
        with pytest.raises(CommandError):
            run_import(
                "import_catalog", write_csv(tmp_path / "e.csv", lines), operator, "--dry-run"
            )

    def test_file_problems(self, tmp_path: Path, operator: str) -> None:
        with pytest.raises(CommandError, match="Required column"):
            run_import(
                "import_catalog", write_csv(tmp_path / "a.csv", ["reference,name", "R,N"]), operator
            )
        with pytest.raises(CommandError, match="not a file"):
            run_import("import_catalog", tmp_path / "missing.csv", operator)


class TestOperator:
    def test_an_operator_without_the_capability_is_refused(
        self, tmp_path: Path, org: Org, people: Any
    ) -> None:
        path = write_csv(tmp_path / "c.csv", GOOD)
        for who in ("tech_mad", "wo_mad", "partner", "revoked", "expired", "nobody"):
            with pytest.raises(CommandError, match=r"does not hold catalog\.manage|active user"):
                run_import("import_catalog", path, getattr(people, who).email)
        assert not Material.objects.exists()

    def test_an_unknown_operator_is_refused(self, tmp_path: Path, org: Org, people: Any) -> None:
        with pytest.raises(CommandError, match="active user"):
            run_import("import_catalog", write_csv(tmp_path / "c.csv", GOOD), "who@example.org")

    def test_a_material_manager_may_import(self, tmp_path: Path, org: Org, people: Any) -> None:
        run_import("import_catalog", write_csv(tmp_path / "c.csv", GOOD), people.mm_mad.email)
        assert Material.objects.count() == 3
        (event,) = events("catalog.imported")
        assert event.actor_label == people.mm_mad.email

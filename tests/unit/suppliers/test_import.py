"""``import_suppliers`` (plan M3.3)."""

from pathlib import Path
from typing import Any

import pytest
from django.core.management.base import CommandError

from apps.audit.models import AuditEvent
from apps.suppliers.models import Supplier
from tests.imports import run_import, write_csv, write_xlsx
from tests.org import Org

pytestmark = pytest.mark.django_db

HEADER = "name,tax_id,contact_name,email,phone,address,notes,active"
GOOD = [
    HEADER,
    "Acme Eléctrica,B1,Marta,Ventas@Acme.example,600,Calle 1,Net 30,",
    "Zeta Cables,B2,,,,,,no",
    "Sin NIF,,,,,,,",
]


@pytest.fixture
def operator(org: Org, people: Any) -> str:
    return str(people.group_admin.email)


class TestImport:
    def test_creates_and_audits_once(self, tmp_path: Path, operator: str, org: Org) -> None:
        out = run_import("import_suppliers", write_csv(tmp_path / "s.csv", GOOD), operator)
        assert "3 created" in out
        acme = Supplier.objects.get(tax_id="B1")
        assert (acme.name, acme.email, acme.phone) == (
            "Acme Eléctrica",
            "ventas@acme.example",
            "600",
        )
        assert not Supplier.objects.get(tax_id="B2").is_active
        assert Supplier.objects.get(name="Sin NIF").tax_id is None
        (event,) = AuditEvent.objects.filter(action="supplier.imported")
        assert event.via == "cli:import_suppliers" and event.data["created"] == 3

    def test_importing_the_same_file_twice_changes_nothing(
        self, tmp_path: Path, operator: str, org: Org
    ) -> None:
        path = write_csv(tmp_path / "s.csv", GOOD)
        run_import("import_suppliers", path, operator)
        stamps = list(Supplier.objects.order_by("name").values_list("pk", "updated_at"))
        audit = AuditEvent.objects.exclude(action="supplier.imported").count()
        out = run_import("import_suppliers", path, operator)
        assert "3 unchanged" in out
        assert list(Supplier.objects.order_by("name").values_list("pk", "updated_at")) == stamps
        assert AuditEvent.objects.exclude(action="supplier.imported").count() == audit

    def test_matching_by_tax_id_then_by_name(self, tmp_path: Path, operator: str, org: Org) -> None:
        run_import("import_suppliers", write_csv(tmp_path / "s.csv", GOOD), operator)
        changed = [
            HEADER,
            "Acme Eléctrica SL,B1,,,,,,",  # same tax id, renamed; blank cells clear
            "sin nif,B3,,,,,,",  # known by name without a tax id: gains one
            "Zeta Cables,,,,,,,yes",  # by name; a blank tax id changes nothing; reactivated
        ]
        out = run_import("import_suppliers", write_csv(tmp_path / "c.csv", changed), operator)
        assert "3 updated" in out
        acme = Supplier.objects.get(tax_id="B1")
        assert (acme.name, acme.email, acme.phone) == ("Acme Eléctrica SL", "", "")
        assert Supplier.objects.get(tax_id="B3").name == "sin nif"  # the file's spelling
        zeta = Supplier.objects.get(name="Zeta Cables")
        assert zeta.tax_id == "B2" and zeta.is_active
        assert Supplier.objects.count() == 4  # + the fixture's sentinel supplier

    def test_a_name_that_belongs_to_another_tax_id_is_refused(
        self, tmp_path: Path, operator: str, org: Org
    ) -> None:
        run_import("import_suppliers", write_csv(tmp_path / "s.csv", GOOD), operator)
        with pytest.raises(CommandError) as caught:
            run_import(
                "import_suppliers",
                write_csv(tmp_path / "c.csv", [HEADER, "Zeta Cables,B9,,,,,,"]),
                operator,
            )
        assert "line 2: name: the name belongs to another supplier (tax id B2)" in (
            caught.value.output  # type: ignore[attr-defined]
        )

    def test_xlsx_with_spanish_headers(self, tmp_path: Path, operator: str, org: Org) -> None:
        path = write_xlsx(
            tmp_path / "s.xlsx",
            [["Proveedor", "CIF", "Teléfono"], ["Nuevo SL", "B7", 600123456]],
        )
        run_import("import_suppliers", path, operator)
        nuevo = Supplier.objects.get(name="Nuevo SL")
        assert (nuevo.tax_id, nuevo.phone) == ("B7", "600123456")


class TestErrors:
    def test_a_file_with_errors_writes_nothing(
        self, tmp_path: Path, operator: str, org: Org
    ) -> None:
        lines = [
            HEADER,
            "Fine,B1,,,,,,",
            ",B2,,,,,,",  # line 3
            "Bad Mail,B3,,not-an-email,,,,",  # line 4
            "Twice,B1,,,,,,",  # line 5: the tax id of line 2
            "Fine Again,,,,,,,maybe",  # line 6
            "fine,,,,,,,",  # line 7: the name of line 2: the same supplier
        ]
        with pytest.raises(CommandError, match="nothing was written") as caught:
            run_import("import_suppliers", write_csv(tmp_path / "e.csv", lines), operator)
        report = caught.value.output  # type: ignore[attr-defined]
        for expected in (
            "line 3: name: is required",
            "line 4: email: 'not-an-email' is not an email address",
            "line 5: is the same as line 2",
            "line 6: active: expected yes or no",
            "line 7: is the same as line 2",
        ):
            assert expected in report, report
        assert Supplier.objects.count() == 1  # only the fixture's
        assert not AuditEvent.objects.filter(action="supplier.imported").exists()

    def test_dry_run(self, tmp_path: Path, operator: str, org: Org) -> None:
        out = run_import(
            "import_suppliers", write_csv(tmp_path / "s.csv", GOOD), operator, "--dry-run"
        )
        assert "3 created" in out and "dry run" in out
        assert Supplier.objects.count() == 1


class TestOperator:
    def test_a_warehouse_operator_may_view_but_not_import(
        self, tmp_path: Path, org: Org, people: Any
    ) -> None:
        for who in ("wo_mad", "tech_mad", "site_manager", "partner", "revoked", "nobody"):
            with pytest.raises(CommandError, match=r"does not hold supplier\.manage|active user"):
                run_import(
                    "import_suppliers",
                    write_csv(tmp_path / "s.csv", GOOD),
                    getattr(people, who).email,
                )
        assert Supplier.objects.count() == 1

    def test_a_material_manager_may(self, tmp_path: Path, org: Org, people: Any) -> None:
        run_import("import_suppliers", write_csv(tmp_path / "s.csv", GOOD), people.mm_sev.email)
        assert Supplier.objects.count() == 4

"""Reading CSV and XLSX files into rows (``apps.core.tabular``)."""

from datetime import date, datetime
from pathlib import Path

import pytest

from apps.core.tabular import (
    MAX_FILE_BYTES,
    Column,
    TableError,
    cell_text,
    normalize_header,
    read_table,
)
from tests.imports import write_csv, write_xlsx

COLUMNS = (
    Column("reference", ("referencia", "ref"), required=True),
    Column("name", ("nombre",), required=True),
    Column("price_note", ("nota",)),
)


class TestHeaders:
    @pytest.mark.parametrize(
        ("header", "expected"),
        [
            ("Referencia", "referencia"),
            ("  Manufacturer  Reference ", "manufacturer_reference"),
            ("código-de_barras", "codigo_de_barras"),
            ("EAN.13", "ean_13"),
        ],
    )
    def test_normalize(self, header: str, expected: str) -> None:
        assert normalize_header(header) == expected

    @pytest.mark.parametrize(
        ("value", "text"),
        [
            (None, ""),
            (True, "true"),
            (False, "false"),
            (7, "7"),
            (2.0, "2"),
            (2.5, "2.5"),
            (0.1, "0.1"),
            (1e20, "100000000000000000000"),
            ("  x ", "x"),
            (date(2026, 3, 1), "2026-03-01"),
            (datetime(2026, 3, 1), "2026-03-01"),
            (datetime(2026, 3, 1, 8, 30), "2026-03-01T08:30:00"),
        ],
    )
    def test_cell_text(self, value: object, text: str) -> None:
        assert cell_text(value) == text


class TestCsv:
    def test_maps_columns_by_name_and_alias(self, tmp_path: Path) -> None:
        path = write_csv(tmp_path / "a.csv", ["Referencia,NOMBRE,Other", "R1,Uno,x", "R2,Dos,y"])
        table = read_table(path, COLUMNS)
        assert table.format == "CSV"
        assert table.mapped == {"reference": "Referencia", "name": "NOMBRE"}
        assert table.ignored == ["Other"]
        assert [(r.line, r.values) for r in table.rows] == [
            (2, {"reference": "R1", "name": "Uno"}),
            (3, {"reference": "R2", "name": "Dos"}),
        ]

    @pytest.mark.parametrize("delimiter", [";", ",", "\t"])
    def test_the_delimiter_comes_from_the_header(self, tmp_path: Path, delimiter: str) -> None:
        lines = [delimiter.join(["reference", "name"]), delimiter.join(["R1", "Uno dos"])]
        table = read_table(write_csv(tmp_path / "a.csv", lines), COLUMNS)
        assert table.rows[0].values == {"reference": "R1", "name": "Uno dos"}

    def test_a_forced_delimiter(self, tmp_path: Path) -> None:
        path = write_csv(tmp_path / "a.csv", ["reference|name", "R1|Uno"])
        assert read_table(path, COLUMNS, delimiter="|").rows[0].get("name") == "Uno"

    def test_windows_1252_and_bom(self, tmp_path: Path) -> None:
        old = write_csv(tmp_path / "old.csv", ["reference;name", "R1;Ñandú"], encoding="cp1252")
        table = read_table(old, COLUMNS)
        assert table.rows[0].get("name") == "Ñandú" and table.encoding == "cp1252"
        bom = write_csv(tmp_path / "bom.csv", ["reference,name", "R1,Ñu"], encoding="utf-8-sig")
        assert read_table(bom, COLUMNS).rows[0].get("name") == "Ñu"

    def test_blank_lines_are_skipped_and_line_numbers_are_the_files(self, tmp_path: Path) -> None:
        path = write_csv(tmp_path / "a.csv", ["reference,name", "", "R1,Uno", ",", "R2,Dos"])
        assert [row.line for row in read_table(path, COLUMNS).rows] == [3, 5]

    def test_short_rows_have_blank_cells(self, tmp_path: Path) -> None:
        path = write_csv(tmp_path / "a.csv", ["reference,name", "R1"])
        assert read_table(path, COLUMNS).rows[0].values == {"reference": "R1", "name": ""}


class TestXlsx:
    def test_reads_values_not_formulas(self, tmp_path: Path) -> None:
        path = write_xlsx(
            tmp_path / "a.xlsx",
            [["Ref", "Nombre", "Nota"], ["R1", "Uno", 2.5], ["R2", "Dos", date(2026, 1, 2)]],
        )
        table = read_table(path, COLUMNS)
        assert table.format == "XLSX"
        assert [row.get("price_note") for row in table.rows] == ["2.5", "2026-01-02"]

    def test_a_named_sheet(self, tmp_path: Path) -> None:
        path = write_xlsx(tmp_path / "a.xlsx", [["reference", "name"], ["R1", "Uno"]], sheet="Data")
        assert read_table(path, COLUMNS, sheet="Data").rows[0].get("reference") == "R1"
        with pytest.raises(TableError, match="no sheet"):
            read_table(path, COLUMNS, sheet="Other")


class TestRefusals:
    def test_a_missing_required_column(self, tmp_path: Path) -> None:
        path = write_csv(tmp_path / "a.csv", ["reference,other", "R1,x"])
        with pytest.raises(TableError, match=r"Required column.*name"):
            read_table(path, COLUMNS)

    def test_a_column_given_twice(self, tmp_path: Path) -> None:
        path = write_csv(tmp_path / "a.csv", ["reference,ref,name", "R1,R1,Uno"])
        with pytest.raises(TableError, match="appears twice"):
            read_table(path, COLUMNS)

    @pytest.mark.parametrize("content", [b"", b"\n\n", b",,\n"])
    def test_no_header(self, tmp_path: Path, content: bytes) -> None:
        path = tmp_path / "a.csv"
        path.write_bytes(content)
        with pytest.raises(TableError, match="no header"):
            read_table(path, COLUMNS)

    def test_not_text(self, tmp_path: Path) -> None:
        path = tmp_path / "a.csv"
        path.write_bytes(b"reference,name\n\x81\x8d\x8f\x90,x\n")
        # cp1252 leaves a few bytes undefined: neither encoding can read them.
        with pytest.raises(TableError, match="not UTF-8"):
            read_table(path, COLUMNS)

    def test_unknown_suffix_missing_file_and_corrupt_xlsx(self, tmp_path: Path) -> None:
        other = tmp_path / "a.pdf"
        other.write_bytes(b"x")
        with pytest.raises(TableError, match=r"\.csv or \.xlsx"):
            read_table(other, COLUMNS)
        with pytest.raises(TableError, match="not a file"):
            read_table(tmp_path / "missing.csv", COLUMNS)
        broken = tmp_path / "b.xlsx"
        broken.write_bytes(b"not a zip archive")
        with pytest.raises(TableError, match="readable XLSX"):
            read_table(broken, COLUMNS)

    def test_too_large(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("apps.core.tabular.MAX_FILE_BYTES", 10)
        path = write_csv(tmp_path / "a.csv", ["reference,name", "R1,Uno"])
        with pytest.raises(TableError, match="larger than"):
            read_table(path, COLUMNS)
        assert MAX_FILE_BYTES == 20 * 1024 * 1024

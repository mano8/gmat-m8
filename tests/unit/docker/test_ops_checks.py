"""``docker_compose/gmat_m8/ops/checks.py``: what the host scripts read from JSON and notes."""

import importlib.util
import json
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

PATH = Path(__file__).resolve().parents[3] / "docker_compose/gmat_m8/ops/checks.py"
DIGEST_A = "sha256:" + "a" * 64
DIGEST_B = "sha256:" + "b" * 64


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("ops_checks", PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    previous = sys.dont_write_bytecode
    sys.dont_write_bytecode = True
    # Its dataclasses and exceptions look their module up while they are defined.
    sys.modules[spec.name] = module
    try:
        spec.loader.exec_module(module)
    finally:
        sys.dont_write_bytecode = previous
    return module


checks = _load()


def manifest(**changes: Any) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "version": "1.2.3",
        "tag": "v1.2.3",
        "commit": "f" * 40,
        "repository": "o/n",
        "images": {
            "app": {
                "repository": "ghcr.io/o/gmat-m8",
                "digest": DIGEST_A,
                "ref": f"ghcr.io/o/gmat-m8@{DIGEST_A}",
            },
            "backup": {
                "repository": "ghcr.io/o/gmat-m8-backup",
                "digest": DIGEST_B,
                "ref": f"ghcr.io/o/gmat-m8-backup@{DIGEST_B}",
            },
        },
        **changes,
    }


def run(*args: str) -> int:
    result: int = checks.main(list(args))
    return result


class TestManifest:
    def test_a_good_one_gives_the_digests(self) -> None:
        values = checks.check_manifest(manifest(), "1.2.3", "o/n", "ghcr.io/o")
        assert values["RELEASE_APP_DIGEST"] == DIGEST_A
        assert values["RELEASE_BACKUP_REF"] == f"ghcr.io/o/gmat-m8-backup@{DIGEST_B}"

    @pytest.mark.parametrize(
        "image",
        [
            {},
            {"repository": "ghcr.io/o/gmat-m8", "digest": "sha256:short", "ref": "x"},
            {
                "repository": "ghcr.io/o/gmat-m8",
                "digest": DIGEST_A,
                "ref": f"ghcr.io/o/other@{DIGEST_A}",
            },
        ],
    )
    def test_a_bad_image_is_refused(self, image: dict[str, str]) -> None:
        document = manifest()
        document["images"]["app"] = image
        with pytest.raises(checks.Refused):
            checks.check_manifest(document, "1.2.3", "o/n", "ghcr.io/o")

    def test_through_the_command_line(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        path = tmp_path / "manifest.json"
        path.write_text(json.dumps(manifest()), encoding="utf-8")
        args = ("manifest", str(path), "--repository", "o/n", "--image-prefix", "ghcr.io/o")
        assert run(*args, "--version", "1.2.3") == 0
        assert f"RELEASE_APP_DIGEST={DIGEST_A}" in capsys.readouterr().out
        assert run(*args, "--version", "1.2.4") == 2
        assert run(*args, "--version", "not-a-version") == 2

    def test_a_file_that_is_not_json(self, tmp_path: Path) -> None:
        path = tmp_path / "manifest.json"
        path.write_text("nope", encoding="utf-8")
        assert (
            run(
                "manifest",
                str(path),
                "--version",
                "1.2.3",
                "--repository",
                "o/n",
                "--image-prefix",
                "p",
            )
            == 2
        )


class TestRelease:
    RELEASE = {"body": "Notes", "assets": [{"name": "a", "url": "https://x/a"}, {"name": "b"}]}

    def test_asset_and_notes(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        path = tmp_path / "release.json"
        path.write_text(json.dumps(self.RELEASE), encoding="utf-8")
        assert run("asset", str(path), "a") == 0
        assert "https://x/a" in capsys.readouterr().out
        assert run("asset", str(path), "b") == 2
        assert run("notes", str(path)) == 0
        assert "Notes" in capsys.readouterr().out

    def test_empty_notes_print_nothing(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        path = tmp_path / "release.json"
        path.write_text(json.dumps({"body": None}), encoding="utf-8")
        assert run("notes", str(path)) == 0
        assert capsys.readouterr().out == ""


class TestFlagged:
    @pytest.mark.parametrize(
        "line",
        [
            "Irreversible migration: drops x",
            "**Irreversible migration:** drops x",
            "- irreversible migration",
            "### Irreversible Migration",
        ],
    )
    def test_a_line_of_its_own(self, line: str, tmp_path: Path) -> None:
        path = tmp_path / "notes.md"
        path.write_text(f"Intro\n{line}\nEnd\n", encoding="utf-8")
        assert run("flagged", str(path), "Irreversible migration") == 0

    @pytest.mark.parametrize("text", ["", "This release has no irreversible migration.\n"])
    def test_anything_else_is_not_a_flag(self, text: str, tmp_path: Path) -> None:
        path = tmp_path / "notes.md"
        path.write_text(text, encoding="utf-8")
        with pytest.raises(SystemExit) as caught:
            run("flagged", str(path), "Irreversible migration")
        assert caught.value.code == 1


class TestAdminCidrs:
    @pytest.mark.parametrize(
        "value", ["203.0.113.0/24", "203.0.113.7 2001:db8::/32", "10.8.0.0/24"]
    )
    def test_named_networks_pass(self, value: str) -> None:
        assert run("admin-cidrs", value) == 0

    @pytest.mark.parametrize(
        "value",
        [
            "",
            "0.0.0.0/0 ::/0",
            "203.0.113.0/24 ::/0",
            "172.17.0.1/32",
            "172.16.0.0/12",
            "172.0.0.0/8",
            "192.168.1.0/24",
            "128.0.0.0/1",
            "not-a-network",
        ],
    )
    def test_open_docker_or_broken_lists_are_refused(self, value: str) -> None:
        assert run("admin-cidrs", value) == 2


class TestBackupManifest:
    def test_the_dump_version(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        path = tmp_path / "m.json"
        path.write_text(json.dumps({"app_version": "1.2.3"}), encoding="utf-8")
        assert run("dump-version", str(path)) == 0
        assert capsys.readouterr().out.strip() == "1.2.3"

    def test_counts_match_except_the_excluded_tables(self, tmp_path: Path) -> None:
        wanted = tmp_path / "m.json"
        found = tmp_path / "r.json"
        wanted.write_text(
            json.dumps(
                {
                    "row_counts": {"a": 1, "django_session": 0},
                    "excluded_data_tables": ["django_session"],
                }
            ),
            encoding="utf-8",
        )
        found.write_text(
            json.dumps({"row_counts": {"a": 1, "django_session": 9}}), encoding="utf-8"
        )
        assert run("compare-counts", str(wanted), str(found)) == 0
        found.write_text(json.dumps({"row_counts": {"a": 2}}), encoding="utf-8")
        assert run("compare-counts", str(wanted), str(found)) == 2

"""``docker_compose/gmat_m8/ops/checks.py``: what the host scripts read from JSON."""

import importlib.util
import io
import json
import re
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
        "migration": {"irreversible": "", "old_code_works": ""},
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

    def test_asset(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        path = tmp_path / "release.json"
        path.write_text(json.dumps(self.RELEASE), encoding="utf-8")
        assert run("asset", str(path), "a") == 0
        assert "https://x/a" in capsys.readouterr().out
        assert run("asset", str(path), "b") == 2


class TestMigrationStatements:
    def _file(self, tmp_path: Path, **changes: Any) -> str:
        path = tmp_path / "manifest.json"
        path.write_text(json.dumps(manifest(**changes)), encoding="utf-8")
        return str(path)

    def _exit_code(self, *args: str) -> int:
        with pytest.raises(SystemExit) as caught:
            run(*args)
        assert isinstance(caught.value.code, int)
        return caught.value.code

    def test_what_the_manifest_states(self, tmp_path: Path) -> None:
        path = self._file(tmp_path, migration={"irreversible": "", "old_code_works": "no change"})
        assert run("states", path, "old_code_works") == 0
        assert self._exit_code("states", path, "irreversible") == 1

    @pytest.mark.parametrize(
        "statements",
        [None, "yes", {}, {"irreversible": ""}, {"irreversible": "", "old_code_works": True}],
    )
    def test_a_manifest_that_does_not_say_is_refused_not_read_as_no(
        self, tmp_path: Path, statements: Any
    ) -> None:
        path = self._file(tmp_path, migration=statements)
        assert run("states", path, "old_code_works") == 2

    @pytest.mark.parametrize(("value", "flag"), [("", "0"), ("drops x", "1")])
    def test_the_deploy_gets_the_irreversible_flag(self, value: str, flag: str) -> None:
        statements = {"irreversible": value, "old_code_works": ""}
        checked = checks.check_manifest(manifest(migration=statements), "1.2.3", "o/n", "ghcr.io/o")
        assert checked["RELEASE_IRREVERSIBLE"] == flag

    def test_an_unknown_statement_is_a_usage_error(self, tmp_path: Path) -> None:
        assert self._exit_code("states", self._file(tmp_path), "other") == 2


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
            "0::/0",  # as open as ::/0, spelled differently
            "::0/0",
            "::/1",
            "2000::/3",
            "0.0.0.0/1",
            "10.0.0.0/7",
            "not-a-network",
        ],
    )
    def test_open_docker_or_broken_lists_are_refused(self, value: str) -> None:
        assert run("admin-cidrs", value) == 2
        assert run("admin-cidrs", value, "--enabled", "true") == 2

    @pytest.mark.parametrize("enabled", ["", "0", "false", "No", "OFF", " off "])
    @pytest.mark.parametrize("value", ["", "0.0.0.0/0 ::/0", "172.17.0.1/32"])
    def test_an_admin_that_is_off_needs_none(
        self, value: str, enabled: str, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """ADR-0030: the words of ``config.env.env_bool``; empty is production's default."""
        assert run("admin-cidrs", value, "--enabled", enabled) == 0
        assert "the admin is off" in capsys.readouterr().out

    @pytest.mark.parametrize("enabled", ["1", "true", "Yes", "ON"])
    def test_an_admin_that_is_on_needs_one(self, enabled: str) -> None:
        assert run("admin-cidrs", "", "--enabled", enabled) == 2
        assert run("admin-cidrs", "203.0.113.0/24", "--enabled", enabled) == 0

    def test_a_word_django_would_refuse_is_refused(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        assert run("admin-cidrs", "203.0.113.0/24", "--enabled", "maybe") == 2
        assert "DJANGO_ADMIN_ENABLED: 'maybe'" in capsys.readouterr().err

    def test_the_words_are_djangos(self) -> None:
        from config import env

        assert env._TRUE == checks.ADMIN_ON
        assert env._FALSE | {""} == checks.ADMIN_OFF


class TestBackupManifest:
    def test_the_dump_time(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        path = tmp_path / "m.json"
        path.write_text(json.dumps({"created_at": "2026-11-04T06:00:00Z"}), encoding="utf-8")
        assert run("dump-time", str(path)) == 0
        assert capsys.readouterr().out.strip() == "2026-11-04T06:00:00Z"
        path.write_text("{}", encoding="utf-8")
        assert run("dump-time", str(path)) == 2

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

    @pytest.mark.parametrize(
        ("command", "printed"),
        [("dump-version", "1.2.3"), ("dump-time", "2026-11-04T06:00:00Z")],
    )
    def test_a_manifest_on_stdin(
        self,
        command: str,
        printed: str,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """``-``: the scripts pipe it from the helper, which alone reads backups/ (ADR-0033)."""
        manifest = {"app_version": "1.2.3", "created_at": "2026-11-04T06:00:00Z"}
        monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(manifest)))
        assert run(command, "-") == 0
        assert capsys.readouterr().out.strip() == printed

    def test_counts_against_a_manifest_on_stdin(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        found = tmp_path / "r.json"
        found.write_text(json.dumps({"row_counts": {"a": 1}}), encoding="utf-8")
        monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps({"row_counts": {"a": 1}})))
        assert run("compare-counts", "-", str(found)) == 0
        monkeypatch.setattr("sys.stdin", io.StringIO("not json"))
        assert run("compare-counts", "-", str(found)) == 2


# --- the host's setup (M12.3): fixtures copied from the real tools (conftest.py) ----------------

FIXTURES = Path(__file__).resolve().parent / "fixtures"


def fixture(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def cmdline(name: str) -> str:
    """A fixture's command line as /proc holds it: each argument ended by a NUL."""
    return "".join(f"{argument}\0" for argument in fixture(name).splitlines())


class TestCosignVersion:
    def test_cosign_3_passes(self) -> None:
        assert checks.cosign_version(fixture("cosign-version-v3.0.6.txt")) == "v3.0.6"

    def test_cosign_2_is_refused(self) -> None:
        with pytest.raises(checks.Refused, match=r"this is cosign v2\.5\.0"):
            checks.cosign_version(fixture("cosign-version-v2.5.0.txt"))

    @pytest.mark.parametrize(
        "text",
        [
            "",
            "GitVersion:    devel\n",
            "GitVersion:\n",
            "  GitVersion:    v3.0.6\n",  # not at the start of a line: not cosign's own field
            "BuildGitVersion: v3.0.6\n",
            "cosign: command not found\n",
        ],
        ids=["empty", "devel", "no value", "indented", "another field", "not cosign"],
    )
    def test_an_output_that_names_no_version_is_refused(self, text: str) -> None:
        with pytest.raises(checks.Refused, match="no GitVersion"):
            checks.cosign_version(text)

    @pytest.mark.parametrize(
        ("line", "found"),
        [("GitVersion:    v10.0.1", "v10.0.1"), ("GitVersion:\t3.1.0-rc.1", "3.1.0-rc.1")],
    )
    def test_any_later_major(self, line: str, found: str) -> None:
        assert checks.cosign_version(f"banner\n{line}\nGitCommit: x\n") == found

    def test_through_the_command_line(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        monkeypatch.setattr("sys.stdin", io.StringIO(fixture("cosign-version-v3.0.6.txt")))
        assert run("cosign-version", "-") == 0
        assert capsys.readouterr().out.strip() == "v3.0.6"
        monkeypatch.setattr("sys.stdin", io.StringIO(fixture("cosign-version-v2.5.0.txt")))
        assert run("cosign-version", "-") == 2

    def test_the_runbook_installs_what_the_preflight_requires(self) -> None:
        """M11: a new cosign major moves the runbook's minimum and this one together."""
        runbook = (PATH.parents[3] / "docs/runbooks/vps-setup.md").read_text(encoding="utf-8")
        installed = re.search(r"^\s*VER=v(\d+)\.\d+\.\d+$", runbook, re.MULTILINE)
        assert installed, "vps-setup.md no longer installs cosign with VER=vX.Y.Z"
        assert int(installed[1]) == checks.COSIGN_MIN_MAJOR


def info(**port: Any) -> str:
    """The real slirp4netns answer with its portDriver changed."""
    answer = json.loads(fixture("rootlesskit-info-slirp4netns.json"))
    answer["portDriver"] = port
    return json.dumps(answer)


class TestPortDriver:
    def test_slirp4netns_from_the_api(self) -> None:
        found = checks.port_driver_from_info(
            json.loads(fixture("rootlesskit-info-slirp4netns.json"))
        )
        assert checks.check_port_driver(*found) == "slirp4netns (tcp, tcp4, udp, udp4)"

    def test_builtin_from_the_api_is_refused_although_it_lists_udp(self) -> None:
        """RootlessKit 3.3.0 lists udp for builtin, which never keeps a UDP client's address."""
        found = checks.port_driver_from_info(json.loads(fixture("rootlesskit-info-builtin.json")))
        assert "udp" in (found[1] or [])
        with pytest.raises(checks.Refused, match=r"builtin .* hides the visitors' addresses"):
            checks.check_port_driver(*found)

    @pytest.mark.parametrize("driver", ["builtin", "gvisor-tap-vsock", "pesto", "none", "vpnkit"])
    def test_every_other_driver_is_refused(self, driver: str) -> None:
        """ADR-0033 point 4: only slirp4netns and implicit, until a measurement says more."""
        with pytest.raises(checks.Refused, match="rate limits would count every visitor as one"):
            checks.check_port_driver(driver, ["tcp", "udp"])

    def test_implicit_passes(self) -> None:
        assert checks.check_port_driver("implicit", ["tcp", "udp"]) == "implicit (tcp, udp)"

    def test_a_driver_without_udp_is_refused(self) -> None:
        found = checks.port_driver_from_info(json.loads(info(driver="slirp4netns", protos=["tcp"])))
        with pytest.raises(checks.Refused, match=r"does not carry UDP: .* 443/udp"):
            checks.check_port_driver(*found)

    def test_protocols_not_stated_are_not_held_against_it(self) -> None:
        found = checks.port_driver_from_info(json.loads(info(driver="slirp4netns")))
        assert found == ("slirp4netns", None)
        assert checks.check_port_driver(*found) == "slirp4netns"

    @pytest.mark.parametrize(
        "answer",
        [{}, [], {"portDriver": None}, {"portDriver": {}}, {"portDriver": {"driver": ""}},
         {"portDriver": {"driver": 3}}],
        ids=["empty", "list", "null", "no driver", "empty driver", "not a name"],
    )  # fmt: skip
    def test_an_api_answer_without_a_driver_is_unknown(self, answer: Any) -> None:
        with pytest.raises(checks.Unknown):
            checks.port_driver_from_info(answer)

    def test_builtin_from_the_command_line(self) -> None:
        found = checks.port_driver_from_cmdline(cmdline("rootlesskit-cmdline-builtin.txt"))
        assert found == ("builtin", None)

    def test_the_last_of_a_repeated_option_counts(self) -> None:
        """The real slirp4netns command line names builtin first, then slirp4netns; RootlessKit
        ran with slirp4netns (its API said so)."""
        text = cmdline("rootlesskit-cmdline-slirp4netns.txt")
        assert text.index("--port-driver=builtin") < text.index("--port-driver=slirp4netns")
        assert checks.port_driver_from_cmdline(text) == ("slirp4netns", None)

    @pytest.mark.parametrize(
        ("arguments", "driver"),
        [
            (["rootlesskit", "--port-driver", "slirp4netns", "dockerd"], "slirp4netns"),
            (["rootlesskit", "--port-driver=slirp4netns", "--", "x", "--port-driver=builtin"],
             "slirp4netns"),
            (["rootlesskit", "--port-driver=builtin", "--port-driver", "implicit"], "implicit"),
        ],
        ids=["separate value", "after --", "both forms"],
    )  # fmt: skip
    def test_how_the_option_is_read(self, arguments: list[str], driver: str) -> None:
        text = "".join(f"{argument}\0" for argument in arguments)
        assert checks.port_driver_from_cmdline(text) == (driver, None)

    @pytest.mark.parametrize(
        "text",
        ["", "rootlesskit\0--net=slirp4netns\0dockerd\0", "rootlesskit\0--port-driver\0",
         "rootlesskit\0--port-driver=\0"],
        ids=["empty", "no option", "no value", "empty value"],
    )  # fmt: skip
    def test_a_command_line_without_a_driver_is_unknown(self, text: str) -> None:
        with pytest.raises(checks.Unknown):
            checks.port_driver_from_cmdline(text)

    @pytest.mark.parametrize(
        ("source", "text", "code", "printed"),
        [
            ("api", fixture("rootlesskit-info-slirp4netns.json"), 0,
             "slirp4netns (tcp, tcp4, udp, udp4)"),
            ("api", fixture("rootlesskit-info-builtin.json"), 2, ""),
            ("api", "not json", 3, ""),
            ("api", "{}", 3, ""),
            ("cmdline", cmdline("rootlesskit-cmdline-slirp4netns.txt"), 0, "slirp4netns"),
            ("cmdline", cmdline("rootlesskit-cmdline-builtin.txt"), 2, ""),
            ("cmdline", "", 3, ""),
        ],
        ids=["api ok", "api refused", "api not json", "api no driver", "cmdline ok",
             "cmdline refused", "cmdline empty"],
    )  # fmt: skip
    def test_through_the_command_line(
        self,
        source: str,
        text: str,
        code: int,
        printed: str,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """0 keeps the address, 2 refused, 3 says nothing (deploy.sh warns); stdin as bytes."""
        monkeypatch.setattr("sys.stdin", io.TextIOWrapper(io.BytesIO(text.encode())))
        assert run("port-driver", "--from", source, "-") == code
        assert capsys.readouterr().out.strip() == printed

    def test_a_file(self, tmp_path: Path) -> None:
        path = tmp_path / "cmdline"
        path.write_bytes(cmdline("rootlesskit-cmdline-slirp4netns.txt").encode())
        assert run("port-driver", "--from", "cmdline", str(path)) == 0
        assert (
            run("port-driver", "--from", "api", str(FIXTURES / "rootlesskit-info-builtin.json"))
            == 2
        )

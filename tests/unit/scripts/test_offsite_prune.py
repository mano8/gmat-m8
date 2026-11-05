"""The maintainer's prune: forged snapshots stop it (plan §9, F61, F72)."""

import datetime
import importlib.util
import json
import sys
from collections.abc import Callable
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

PATH = Path(__file__).resolve().parents[3] / "scripts" / "offsite_prune.py"


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("offsite_prune", PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    # Its dataclasses look their module up while they are defined.
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


prune = _load()
NOW = datetime.datetime(2026, 11, 5, 12, 0, tzinfo=datetime.UTC)


def snapshot(
    age_days: float = 1,
    *,
    host: str = "gmat-m8",
    tags: tuple[str, ...] = ("gmat-m8", "dump:gmat-20261105T060000Z"),
    id_: str = "a" * 64,
) -> dict[str, Any]:
    when = NOW - datetime.timedelta(days=age_days)
    return {
        "id": id_,
        "short_id": id_[:8],
        "time": when.strftime("%Y-%m-%dT%H:%M:%S.123456789Z"),
        "hostname": host,
        "tags": list(tags),
    }


class Fake:
    """restic with a repository of ``snapshots``, and a plan for ``forget --dry-run``.

    ``forget --prune`` removes what it names, unless ``append_only``: then, like the real thing
    behind an append-only server, it removes nothing and still exits 0.
    """

    def __init__(
        self,
        snapshots: list[dict[str, Any]],
        remove: list[dict[str, Any]] | None = None,
        *,
        append_only: bool = False,
    ):
        self.snapshots = snapshots
        self.remove = remove or []
        self.append_only = append_only
        self.calls: list[list[str]] = []

    def __call__(self, arguments: list[str]) -> str:
        self.calls.append(arguments)
        if arguments[0] == "snapshots":
            return json.dumps(self.snapshots)
        if "--dry-run" in arguments:
            return json.dumps([{"keep": self.snapshots, "remove": self.remove}])
        if not self.append_only:
            self.snapshots = [item for item in self.snapshots if item["id"] not in arguments]
        return ""

    @property
    def forgot(self) -> list[list[str]]:
        return [call for call in self.calls if call[:2] == ["forget", "--prune"]]


def run(fake: Fake, **kwargs: Any) -> int:
    result: int = prune.prune(fake, NOW, dry_run=kwargs.pop("dry_run", False), **kwargs)
    return result


class TestForged:
    @pytest.mark.parametrize(
        ("snapshot_", "says"),
        [
            (snapshot(-30), "timestamp in the future"),
            (snapshot(host="attacker"), "unexpected host"),
            (snapshot(tags=("dump:gmat-20261105T060000Z",)), "no 'gmat-m8' tag"),
            (snapshot(tags=("gmat-m8", "dump:evil")), "unexpected tag"),
            (snapshot(tags=("gmat-m8", "other")), "unexpected tag"),
            (snapshot(tags=()), "no 'gmat-m8' tag"),
        ],
        ids=["future", "host", "tag missing", "dump tag", "other tag", "no tags"],
    )
    def test_refuses_and_forgets_nothing(
        self, snapshot_: dict[str, Any], says: str, capsys: pytest.CaptureFixture[str]
    ) -> None:
        fake = Fake([snapshot(), snapshot_])
        assert run(fake) == 2
        out = capsys.readouterr().out
        assert "REFUSED" in out and says in out
        assert fake.calls == [["snapshots", "--json"]]

    def test_too_many_in_one_day(self, capsys: pytest.CaptureFixture[str]) -> None:
        fake = Fake([snapshot(1) for _ in range(prune.MAX_PER_DAY + 1)])
        assert run(fake) == 2
        assert "snapshots in one day" in capsys.readouterr().out

    def test_a_clock_a_little_ahead_is_no_forgery(self) -> None:
        assert run(Fake([snapshot(-0.001)])) == 0


class TestThePlan:
    def test_a_policy_that_would_remove_the_protected_window_is_refused(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        fake = Fake([snapshot(1), snapshot(89)], remove=[snapshot(89)])
        assert run(fake) == 2
        assert "last 90 days" in capsys.readouterr().out
        assert not fake.forgot

    def test_only_what_was_checked_is_forgotten(self) -> None:
        old = snapshot(200, id_="b" * 64)
        fake = Fake([snapshot(1), old], remove=[old])
        assert run(fake) == 0
        assert fake.forgot == [["forget", "--prune", "b" * 64]]

    def test_a_prune_that_removed_nothing_is_not_called_a_success(self) -> None:
        """restic exits 0 when an append-only server refuses: the listing decides."""
        old = snapshot(200, id_="b" * 64)
        fake = Fake([snapshot(1), old], remove=[old], append_only=True)
        with pytest.raises(prune.ResticFailed, match="delete-capable"):
            run(fake)

    def test_a_dry_run_forgets_nothing(self) -> None:
        old = snapshot(200)
        fake = Fake([snapshot(1), old], remove=[old])
        assert run(fake, dry_run=True) == 0
        assert not fake.forgot

    def test_nothing_to_remove_prunes_nothing(self) -> None:
        fake = Fake([snapshot(1)])
        assert run(fake) == 0
        assert not fake.forgot

    def test_the_policy_is_the_plans_and_groups_by_host_alone(self) -> None:
        arguments = prune.policy_arguments()
        joined = " ".join(arguments)
        assert "--keep-within 90d" in joined
        assert "--keep-weekly 4" in joined
        assert "--keep-monthly 12" in joined
        assert "--group-by host" in joined, "each snapshot's paths differ: the default keeps all"

    def test_an_empty_repository(self) -> None:
        assert run(Fake([])) == 0

    def test_restic_null_answers_mean_nothing(self) -> None:
        assert prune.parse_snapshots("null") == []
        assert prune.planned_removals("null") == []


class TestCommandLine:
    @pytest.fixture
    def restic(self, monkeypatch: pytest.MonkeyPatch) -> Callable[[list[Any]], None]:
        def install(answers: list[Any]) -> None:
            def run_it(command: list[str], **kwargs: Any) -> Any:
                answer = answers.pop(0)
                if isinstance(answer, Exception):
                    raise answer
                return type("Done", (), answer)

            monkeypatch.setattr(prune.subprocess, "run", run_it)

        return install

    def test_a_full_run(
        self, restic: Callable[[list[Any]], None], capsys: pytest.CaptureFixture[str]
    ) -> None:
        now = datetime.datetime.now(datetime.UTC)
        item = {
            "id": "c" * 64,
            "time": now.isoformat(),
            "hostname": "gmat-m8",
            "tags": ["gmat-m8"],
        }
        restic(
            [
                {"returncode": 0, "stdout": json.dumps([item]), "stderr": ""},
                {"returncode": 0, "stdout": "[]", "stderr": ""},
            ]
        )
        assert prune.main([]) == 0
        assert "1 snapshots, 0 to remove" in capsys.readouterr().out

    def test_restic_failing_is_not_a_refusal(
        self, restic: Callable[[list[Any]], None], capsys: pytest.CaptureFixture[str]
    ) -> None:
        restic([{"returncode": 1, "stdout": "", "stderr": "wrong password"}])
        assert prune.main(["--dry-run"]) == 1
        assert "wrong password" in capsys.readouterr().err

    def test_restic_missing(self, restic: Callable[[list[Any]], None]) -> None:
        restic([FileNotFoundError("restic")])
        assert prune.main([]) == 1

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


@pytest.fixture(autouse=True)
def _state_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(prune, "DEFAULT_STATE", tmp_path / "seen.json")


def run(fake: Fake, **kwargs: Any) -> int:
    """A run that has its baseline, unless the test says otherwise."""
    kwargs.setdefault("state", prune.DEFAULT_STATE)
    kwargs.setdefault("baseline", True)
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


class TestBackDated:
    """A forgery with the right host and tags, dated in the past (security review, M8): nothing
    about the snapshot gives it away, so this machine's memory of the last prune has to."""

    def _pruned_once(self, *snapshots: dict[str, Any]) -> Fake:
        fake = Fake(list(snapshots))
        assert run(fake) == 0
        return fake

    def test_a_snapshot_new_since_the_last_prune_and_dated_before_it_is_refused(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        fake = self._pruned_once(snapshot(1))
        fake.snapshots.append(snapshot(200, id_="f" * 64))
        fake.remove = [snapshot(150, id_="e" * 64)]  # what the forgery would push out
        assert run(fake, baseline=False) == 2
        assert fake.forgot == []
        out = capsys.readouterr().out
        assert "new since the last prune but dated before it" in out
        assert "ffffffff" in out

    def test_a_baseline_flag_does_not_wave_it_through(self) -> None:
        fake = self._pruned_once(snapshot(1))
        fake.snapshots.append(snapshot(200, id_="f" * 64))
        assert run(fake, baseline=True) == 2

    def test_what_was_there_at_the_last_prune_is_known_however_old(self) -> None:
        fake = self._pruned_once(snapshot(300, id_="b" * 64), snapshot(1))
        assert run(fake, baseline=False) == 0

    def test_a_new_snapshot_after_the_last_prune_passes(self) -> None:
        fake = self._pruned_once(snapshot(1))
        fake.snapshots.append(snapshot(-0.001, id_="c" * 64))
        assert run(fake, baseline=False) == 0

    def test_a_backup_uploaded_just_after_the_last_prune_passes(self) -> None:
        """It started before that prune and was listed only after it."""
        fake = self._pruned_once(snapshot(1))
        fake.snapshots.append(snapshot(0.5, id_="c" * 64))
        assert run(fake, baseline=False) == 0

    def test_the_first_run_needs_a_baseline_and_shows_what_it_would_trust(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        fake = Fake([snapshot(1)], [snapshot(200, id_="b" * 64)])
        assert run(fake, baseline=False) == 2
        assert fake.forgot == []
        out = capsys.readouterr().out
        assert "--baseline" in out and "aaaaaaaa" in out
        assert not prune.DEFAULT_STATE.exists()

    def test_a_first_run_on_an_empty_repository_says_so(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        assert run(Fake([]), baseline=False) == 2
        assert "(no snapshots)" in capsys.readouterr().out

    def test_a_dry_run_records_nothing(self) -> None:
        assert run(Fake([snapshot(1)]), dry_run=True) == 0
        assert not prune.DEFAULT_STATE.exists()

    def test_a_refused_run_records_nothing(self) -> None:
        fake = self._pruned_once(snapshot(1))
        before = prune.DEFAULT_STATE.read_text()
        fake.snapshots.append(snapshot(200, id_="f" * 64))
        assert run(fake) == 2
        assert prune.DEFAULT_STATE.read_text() == before

    def test_what_a_prune_removed_is_forgotten_and_the_file_is_private(self) -> None:
        old = snapshot(200, id_="b" * 64)
        fake = Fake([old, snapshot(1)], [old])
        assert run(fake) == 0
        recorded = json.loads(prune.DEFAULT_STATE.read_text())
        assert recorded["ids"] == ["a" * 64]
        assert recorded["at"] == NOW.isoformat()
        assert prune.DEFAULT_STATE.stat().st_mode & 0o777 == 0o600

    def test_a_restic_that_removes_nothing_records_nothing(self) -> None:
        old = snapshot(200, id_="b" * 64)
        fake = Fake([old, snapshot(1)], [old], append_only=True)
        with pytest.raises(prune.ResticFailed):
            run(fake)
        assert not prune.DEFAULT_STATE.exists()


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
        assert prune.main(["--baseline", "--state", str(prune.DEFAULT_STATE)]) == 0
        assert "1 snapshots, 0 to remove" in capsys.readouterr().out
        assert prune.DEFAULT_STATE.exists()

    def test_restic_failing_is_not_a_refusal(
        self, restic: Callable[[list[Any]], None], capsys: pytest.CaptureFixture[str]
    ) -> None:
        restic([{"returncode": 1, "stdout": "", "stderr": "wrong password"}])
        assert prune.main(["--dry-run", "--state", str(prune.DEFAULT_STATE)]) == 1
        assert "wrong password" in capsys.readouterr().err

    def test_restic_missing(self, restic: Callable[[list[Any]], None]) -> None:
        restic([FileNotFoundError("restic")])
        assert prune.main(["--state", str(prune.DEFAULT_STATE)]) == 1

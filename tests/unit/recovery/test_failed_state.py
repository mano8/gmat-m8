"""The file ``ops/failed-state.sql`` writes, and the actions it has to carry."""

import json
import re
from pathlib import Path

import pytest

from apps.core.ids import new_id
from apps.recovery import failed_state
from apps.recovery.replay import HANDLERS

SQL = Path(__file__).resolve().parents[3] / "docker_compose/gmat_m8/ops/failed-state.sql"


def test_parses_what_the_export_writes_in_log_order() -> None:
    first, second = new_id(), new_id()
    message = new_id()
    raw = json.dumps(
        {
            "format_version": 1,
            "exported_at": "2026-11-05T10:00:00+00:00",
            "audit": [
                {
                    "id": str(second),
                    "occurred_at": "2026-11-05T09:30:00.5+00:00",
                    "actor_label": "a@example.org",
                    "action": "user.mfa_reset",
                    "object_type": "accounts.user",
                    "object_id": None,
                    "data": None,
                },
                {
                    "id": str(first),
                    "occurred_at": "2026-11-05T09:00:00+00:00",
                    "actor_label": "a@example.org",
                    "action": "membership.revoked",
                    "object_type": "organizations.membership",
                    "object_id": str(new_id()),
                    "data": {"role": "TECHNICIAN"},
                },
            ],
            "outbox": [
                {"id": str(message), "status": "SENT", "sent_at": "2026-11-05T09:10:00+00:00"},
                {"id": str(new_id()), "status": "CANCELLED", "sent_at": None},
            ],
        }
    )
    state = failed_state.parse(raw)
    assert [row.id for row in state.audit] == [first, second]
    assert state.audit[0].data == {"role": "TECHNICIAN"}
    assert state.audit[1].object_id is None
    assert state.audit[1].data == {}
    assert state.outbox[0].id == message
    assert state.outbox[0].sent_at is not None
    assert state.outbox[1].sent_at is None


@pytest.mark.parametrize(
    "raw",
    [
        "",
        "not json",
        "[]",
        json.dumps({"format_version": 2, "audit": [], "outbox": []}),
        json.dumps({"format_version": 1, "audit": []}),
        json.dumps({"format_version": 1, "audit": [{"id": "not-a-uuid"}], "outbox": []}),
        json.dumps(
            {
                "format_version": 1,
                "audit": [
                    {
                        "id": str(new_id()),
                        "occurred_at": 12,
                        "actor_label": "",
                        "action": "x.y",
                        "object_type": "",
                        "object_id": None,
                        "data": {},
                    }
                ],
                "outbox": [],
            }
        ),
    ],
)
def test_anything_else_is_refused(raw: str) -> None:
    with pytest.raises(failed_state.InvalidFailedState):
        failed_state.parse(raw)


def test_the_export_covers_every_action_the_replay_handles() -> None:
    sql = SQL.read_text(encoding="utf-8")
    families = re.search(r"action ~ '\^\(([a-z_|]+)\)\\\.'", sql)
    assert families, "the export filters on action families"
    exported = set(families[1].split("|"))
    assert {action.split(".")[0] for action in HANDLERS} <= exported
    for status in ("SENT", "CANCELLED", "SUPERSEDED", "SUPPRESSED", "EXPIRED"):
        assert f"'{status}'" in sql

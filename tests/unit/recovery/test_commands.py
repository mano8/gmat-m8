"""The commands a recovery runs: reconcile, confirm accounts, release what is held."""

import io
import json
from collections.abc import Callable
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError

from apps.access.exceptions import AccessDenied
from apps.access.roles import Role
from apps.accounts.models import User
from apps.audit.models import AuditEvent
from apps.core.ids import new_id
from apps.notifications import services
from apps.notifications.models import EmailOutbox, Kind, Status
from apps.notifications.outbox import queue_notification
from apps.organizations.models import Membership
from tests.org import Org, People, build_sentinel_order
from tests.outbox import make_dispatch
from tests.unit.recovery.conftest import audit_event

pytestmark = pytest.mark.django_db


def run(name: str, *args: Any, stdin: str | None = None) -> str:
    out = io.StringIO()
    with patch("sys.stdin", io.StringIO(stdin or "")):
        call_command(name, *map(str, args), stdout=out)
    return out.getvalue()


def state_file(*events: dict[str, Any]) -> str:
    return json.dumps({"format_version": 1, "audit": list(events), "outbox": []})


class TestReconcile:
    def test_a_failed_state_from_standard_input_is_replayed_and_reported_as_json(
        self, operator: User, people: People
    ) -> None:
        membership = Membership.objects.get(user=people.tech_mad)
        out = run(
            "post_restore_reconcile",
            "--operator",
            operator.email,
            "--failed-state",
            "-",
            stdin=state_file(audit_event("membership.revoked", membership.pk)),
        )
        report = json.loads(out)
        assert report["replayed"] == {"membership.revoked": 1}
        assert report["failed_state_used"] is True
        event = AuditEvent.objects.get(action="recovery.reconciled")
        assert event.via == "cli:post_restore_reconcile"

    def test_a_failed_state_from_a_file(self, operator: User, tmp_path: Path) -> None:
        path = tmp_path / "failed-state.json"
        path.write_text(state_file(), encoding="utf-8")
        out = run("post_restore_reconcile", "--operator", operator.email, "--failed-state", path)
        assert json.loads(out)["failed_state_used"] is True

    def test_no_failed_state_takes_the_listed_accounts(
        self, operator: User, people: People
    ) -> None:
        out = run(
            "post_restore_reconcile",
            "--operator",
            operator.email,
            "--no-failed-state",
            "--gap-offboarded",
            people.tech_mad.email,
            "--gap-credentials-changed",
            people.wo_mad.email,
        )
        report = json.loads(out)
        assert report["failed_state_used"] is False
        assert (report["gap_offboarded"], report["gap_credentials_reset"]) == (1, 1)
        assert people.mm_mad.email in report["suspended"]

    def test_one_of_the_two_is_required(self, operator: User) -> None:
        with pytest.raises(CommandError):
            run("post_restore_reconcile", "--operator", operator.email)

    def test_a_gap_next_to_a_failed_state_is_refused(self, operator: User, people: People) -> None:
        with pytest.raises(CommandError, match="leave out"):
            run(
                "post_restore_reconcile",
                "--operator",
                operator.email,
                "--failed-state",
                "-",
                "--gap-offboarded",
                people.tech_mad.email,
                stdin=state_file(),
            )

    @pytest.mark.parametrize("content", ["not json", '{"format_version": 9}'])
    def test_a_bad_file_writes_nothing(self, operator: User, content: str) -> None:
        with pytest.raises(CommandError, match="not a failed-state file"):
            run(
                "post_restore_reconcile",
                "--operator",
                operator.email,
                "--failed-state",
                "-",
                stdin=content,
            )
        assert not AuditEvent.objects.filter(action="recovery.reconciled").exists()

    def test_a_missing_file_is_refused(self, operator: User, tmp_path: Path) -> None:
        with pytest.raises(CommandError):
            run(
                "post_restore_reconcile",
                "--operator",
                operator.email,
                "--failed-state",
                tmp_path / "gone.json",
            )

    def test_an_address_nobody_has_is_refused(self, operator: User) -> None:
        with pytest.raises(CommandError, match=r"typo@example\.org"):
            run(
                "post_restore_reconcile",
                "--operator",
                operator.email,
                "--no-failed-state",
                "--gap-offboarded",
                "typo@example.org",
            )

    def test_a_group_administrator_is_refused_while_an_operator_exists(
        self, people: People
    ) -> None:
        with pytest.raises(CommandError, match="may not reconcile"):
            run(
                "post_restore_reconcile",
                "--operator",
                people.group_admin.email,
                "--no-failed-state",
            )


class TestConfirmAccount:
    @pytest.fixture
    def suspended(self, operator: User) -> None:
        run("post_restore_reconcile", "--operator", operator.email, "--no-failed-state")

    def test_lists_then_confirms(self, operator: User, people: People, suspended: None) -> None:
        listed = run("confirm_account", "--operator", operator.email, "--list")
        assert people.mm_mad.email in listed
        out = run("confirm_account", "--operator", operator.email, "--email", people.mm_mad.email)
        assert "can sign in again" in out
        people.mm_mad.refresh_from_db()
        assert people.mm_mad.is_active
        assert people.mm_mad.email not in run(
            "confirm_account", "--operator", operator.email, "--list"
        )

    def test_credentials_that_changed(
        self, operator: User, people: People, suspended: None
    ) -> None:
        run(
            "confirm_account",
            "--operator",
            operator.email,
            "--email",
            people.mm_mad.email,
            "--credentials-changed",
        )
        people.mm_mad.refresh_from_db()
        assert not people.mm_mad.has_usable_password()

    def test_nobody_has_that_address(self, operator: User) -> None:
        with pytest.raises(CommandError, match="No account"):
            run("confirm_account", "--operator", operator.email, "--email", "x@example.org")

    def test_not_suspended(self, operator: User, people: People) -> None:
        with pytest.raises(CommandError, match="not suspended"):
            run("confirm_account", "--operator", operator.email, "--email", people.tech_mad.email)

    def test_a_staff_account_needs_a_platform_operator(
        self,
        operator: User,
        people: People,
        suspended: None,
        member: Callable[..., User],
    ) -> None:
        admin = member(Role.GROUP_ADMIN)
        with pytest.raises(CommandError, match="may not confirm"):
            run("confirm_account", "--operator", admin.email, "--email", people.operator.email)


class TestOutboxQuarantine:
    @pytest.fixture
    def held(self, operator: User, org: Org, people: People) -> dict[str, EmailOutbox]:
        order = build_sentinel_order(org, people).order
        [notification] = queue_notification(
            recipients=[people.tech_mad.pk],
            capability="catalog.view",
            obj=None,
            subject="S",
            body="B",
        )
        dispatch = make_dispatch(order, Status.QUEUED)
        run(
            "post_restore_reconcile",
            "--operator",
            operator.email,
            "--failed-state",
            "-",
            stdin=state_file(),
        )
        return {"notification": notification, "dispatch": dispatch}

    def test_lists_what_is_held(self, operator: User, held: dict[str, EmailOutbox]) -> None:
        out = run("outbox_quarantine", "--operator", operator.email, "list")
        assert f"{Kind.NOTIFICATION}: 1" in out
        assert f"{Kind.PO_ORDER}: 1" in out
        assert str(held["dispatch"].pk) in out
        assert "@" not in out, "no address on the screen"

    def test_notifications_go_back_to_the_queue_in_bulk(
        self, operator: User, held: dict[str, EmailOutbox]
    ) -> None:
        out = run("outbox_quarantine", "--operator", operator.email, "release-notifications")
        assert "1 notifications queued" in out
        held["notification"].refresh_from_db()
        held["dispatch"].refresh_from_db()
        assert held["notification"].status == Status.QUEUED
        assert held["dispatch"].status == Status.QUARANTINED
        assert AuditEvent.objects.get(action="outbox.quarantine_released").data == {"messages": 1}

    def test_notifications_are_discarded_in_bulk(
        self, operator: User, held: dict[str, EmailOutbox]
    ) -> None:
        out = run("outbox_quarantine", "--operator", operator.email, "discard-notifications")
        assert "1 notifications discarded" in out
        held["notification"].refresh_from_db()
        assert held["notification"].status == Status.CANCELLED
        assert AuditEvent.objects.get(action="outbox.quarantine_discarded").data == {"messages": 1}

    @pytest.mark.parametrize(
        ("action", "status"),
        [
            ("confirm-dispatch", Status.SENT),
            ("release-dispatch", Status.QUEUED),
            ("discard-dispatch", Status.CANCELLED),
        ],
    )
    def test_a_dispatch_is_decided_one_by_one(
        self, operator: User, held: dict[str, EmailOutbox], action: str, status: Status
    ) -> None:
        dispatch = held["dispatch"]
        out = run(
            "outbox_quarantine",
            "--operator",
            operator.email,
            action,
            str(dispatch.pk),
            "--note",
            "Called them",
        )
        dispatch.refresh_from_db()
        assert dispatch.status == status
        assert str(dispatch.pk) in out
        event = AuditEvent.objects.filter(data__dispatch_id=str(dispatch.pk)).latest("occurred_at")
        assert event.data["note"] == "Called them"
        held["notification"].refresh_from_db()
        assert held["notification"].status == Status.QUARANTINED

    def test_a_dispatch_action_needs_an_id(self, operator: User, held: Any) -> None:
        with pytest.raises(CommandError, match="needs the dispatch"):
            run("outbox_quarantine", "--operator", operator.email, "discard-dispatch")

    def test_an_unknown_dispatch(self, operator: User) -> None:
        with pytest.raises(CommandError, match="No such dispatch"):
            run(
                "outbox_quarantine", "--operator", operator.email, "discard-dispatch", str(new_id())
            )

    def test_a_dispatch_that_is_not_held(self, operator: User, org: Org, people: People) -> None:
        order = build_sentinel_order(org, people).order
        message = make_dispatch(order, Status.QUEUED)
        with pytest.raises(CommandError, match="quarantined"):
            run(
                "outbox_quarantine",
                "--operator",
                operator.email,
                "discard-dispatch",
                str(message.pk),
            )

    def test_who_holds_no_grant_is_refused(self, people: People) -> None:
        with pytest.raises(CommandError):
            run("outbox_quarantine", "--operator", people.tech_mad.email, "list")

    def test_a_group_administrator_who_lost_it_between_steps(
        self, operator: User, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def refuse(*args: Any) -> int:
            raise AccessDenied("revoked meanwhile")

        monkeypatch.setattr(services, "release_quarantined_notifications", refuse)
        with pytest.raises(CommandError, match="may not do this"):
            run("outbox_quarantine", "--operator", operator.email, "release-notifications")

"""The in-app notifications themselves (M17.3, Q98, Q99): the writer, what a person sees and
counts, opening and marking read, and the purge.

A person sees and counts only their own rows whose request they may still see with the notice's
capability; another person's notification, or one about a request that left their scope, is a
404 to the services. The purge deletes by Q98's rules in bounded batches within a time budget.
"""

import logging
from collections.abc import Callable, Iterator
from datetime import timedelta
from typing import Any

import pytest
from django.db import connection, transaction
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone

from apps.access.context import AccessContext
from apps.access.exceptions import AccessDenied, MFARequired, NotInScope
from apps.access.principals import SystemPrincipal
from apps.accounts.models import User
from apps.material_requests.models import MaterialRequest
from apps.notifications import housekeeping, in_app, selectors
from apps.notifications.kinds import SUBJECT, Channel, Notice
from apps.notifications.models import ChannelSwitch, InAppNotification
from apps.notifications.outbox import OutboxOutsideTransaction
from apps.notifications.services import mark_all_read, open_notification, run_housekeeping
from apps.organizations.models import Membership
from tests.org import Org, People
from tests.support import principal_for
from tests.unit.material_requests.conftest import MakeRequest

pytestmark = pytest.mark.django_db

DAY = timedelta(days=1)


@pytest.fixture
def request_(make_request: MakeRequest) -> MaterialRequest:
    """A submitted hospital request of A-MAD's technician."""
    return make_request(MaterialRequest.Status.SUBMITTED)


@pytest.fixture
def note() -> Callable[..., InAppNotification]:
    def make(
        user: User, request: MaterialRequest, notice: Notice = Notice.REQUEST_APPROVED, **extra: Any
    ) -> InAppNotification:
        return InAppNotification.objects.create(
            user=user, notice=notice, object_type=SUBJECT, object_id=request.pk, **extra
        )

    return make


def ctx(user: User) -> AccessContext:
    return AccessContext.for_user(User.objects.get(pk=user.pk))


class TestNotify:
    def test_writes_a_row_for_who_wants_it(self, people: People, request_: MaterialRequest) -> None:
        ChannelSwitch.objects.create(user=people.mm_mad, channel=Channel.IN_APP, enabled=False)
        told = in_app.notify(Notice.REQUEST_TO_REVIEW, [people.mm_mad, people.engineer], request_)
        assert told == 1
        (row,) = InAppNotification.objects.all()
        assert (row.user_id, row.notice) == (people.engineer.pk, Notice.REQUEST_TO_REVIEW)
        assert (row.object_type, row.object_id) == (SUBJECT, request_.pk)
        assert row.notified_at is not None and row.read_at is None

    def test_nobody_wanting_it_writes_nothing(
        self, people: People, request_: MaterialRequest
    ) -> None:
        assert in_app.notify(Notice.REQUEST_TO_REVIEW, [], request_) == 0
        assert not InAppNotification.objects.exists()

    def test_an_unknown_notice_is_refused(self, people: People, request_: MaterialRequest) -> None:
        with pytest.raises(ValueError, match="Unknown notice"):
            in_app.notify("NOT_A_NOTICE", [people.tech_mad], request_)  # type: ignore[arg-type]

    def test_only_what_notices_are_about(self, org: Org, people: People) -> None:
        with pytest.raises(TypeError):
            in_app.notify(Notice.REQUEST_APPROVED, [people.tech_mad], org.hospital)

    @pytest.mark.django_db(transaction=True)
    def test_only_inside_the_business_transaction(self, org: Org, people: People) -> None:
        with pytest.raises(OutboxOutsideTransaction):
            in_app.notify(Notice.REQUEST_APPROVED, [people.tech_mad], org.hospital)

    def test_a_grouped_notice_refreshes_the_unread_row(
        self, people: People, request_: MaterialRequest, note: Callable[..., InAppNotification]
    ) -> None:
        old = note(people.tech_mad, request_, Notice.REQUEST_EDITED)
        InAppNotification.objects.filter(pk=old.pk).update(notified_at=timezone.now() - DAY)
        in_app.notify(Notice.REQUEST_EDITED, [people.tech_mad], request_)
        (row,) = InAppNotification.objects.all()
        assert row.pk == old.pk and row.notified_at > timezone.now() - DAY / 2

    def test_a_row_names_who_what_and_its_state(
        self, people: People, request_: MaterialRequest, note: Callable[..., InAppNotification]
    ) -> None:
        row = note(people.tech_mad, request_)
        assert str(row) == f"{people.tech_mad.pk} REQUEST_APPROVED {request_.pk}: unread"
        row.read_at = timezone.now()
        assert str(row).endswith(": read")

    def test_a_notice_not_grouped_adds_a_row(
        self, people: People, request_: MaterialRequest, note: Callable[..., InAppNotification]
    ) -> None:
        note(people.tech_mad, request_, Notice.REQUEST_HANDED_OVER)
        in_app.notify(Notice.REQUEST_HANDED_OVER, [people.tech_mad], request_)
        assert InAppNotification.objects.count() == 2

    def test_one_unread_grouped_row_per_request_is_a_constraint(
        self, people: People, request_: MaterialRequest, note: Callable[..., InAppNotification]
    ) -> None:
        from django.db import IntegrityError

        note(people.tech_mad, request_, Notice.REQUEST_EDITED)
        with pytest.raises(IntegrityError), transaction.atomic():
            note(people.tech_mad, request_, Notice.REQUEST_EDITED)
        note(people.tech_mad, request_, Notice.REQUEST_EDITED, read_at=timezone.now())


class TestVisible:
    def test_only_the_persons_own_rows(
        self, people: People, request_: MaterialRequest, note: Callable[..., InAppNotification]
    ) -> None:
        mine = note(people.tech_mad, request_)
        note(people.mm_mad, request_)
        assert list(in_app.visible(ctx(people.tech_mad))) == [mine]

    def test_nothing_for_a_signed_out_context(
        self, people: People, request_: MaterialRequest, note: Callable[..., InAppNotification]
    ) -> None:
        note(people.tech_mad, request_)
        assert list(in_app.visible(AccessContext.anonymous())) == []

    def test_a_row_needs_the_notices_capability_on_the_request(
        self, people: People, request_: MaterialRequest, note: Callable[..., InAppNotification]
    ) -> None:
        note(people.tech_mad, request_, Notice.REQUEST_TO_REVIEW)  # a technician cannot review
        assert not in_app.visible(ctx(people.tech_mad)).exists()
        assert selectors.unread_count(ctx(people.tech_mad)).count == 0

    def test_a_request_out_of_scope_is_neither_shown_nor_counted(
        self,
        org: Org,
        people: People,
        make_request: MakeRequest,
        note: Callable[..., InAppNotification],
    ) -> None:
        elsewhere = make_request(
            MaterialRequest.Status.SUBMITTED, project=org.sev_works, user=people.mm_sev
        )
        note(people.tech_mad, elsewhere)
        assert selectors.unread_count(ctx(people.tech_mad)).count == 0
        assert list(selectors.in_app_notifications(ctx(people.tech_mad))) == []

    def test_a_partner_never_sees_a_row_about_someone_elses_request(
        self, people: People, request_: MaterialRequest, note: Callable[..., InAppNotification]
    ) -> None:
        note(people.partner, request_)
        assert not in_app.visible(ctx(people.partner)).exists()

    def test_lost_access_hides_what_was_delivered(
        self, people: People, request_: MaterialRequest, note: Callable[..., InAppNotification]
    ) -> None:
        note(people.tech_mad, request_)
        assert selectors.unread_count(ctx(people.tech_mad)).count == 1
        Membership.objects.filter(user=people.tech_mad).update(revoked_at=timezone.now())
        assert selectors.unread_count(ctx(people.tech_mad)).count == 0
        assert list(selectors.in_app_notifications(ctx(people.tech_mad))) == []


class TestSelectors:
    def test_the_count_stops_at_99_plus(
        self, people: People, request_: MaterialRequest, note: Callable[..., InAppNotification]
    ) -> None:
        InAppNotification.objects.bulk_create(
            InAppNotification(
                user=people.tech_mad,
                notice=Notice.REQUEST_HANDED_OVER,
                object_type=SUBJECT,
                object_id=request_.pk,
            )
            for _ in range(105)
        )
        unread = selectors.unread_count(ctx(people.tech_mad))
        assert (unread.count, unread.more, unread.label) == (99, True, "99+")
        InAppNotification.objects.filter(
            pk__in=InAppNotification.objects.values("pk")[:100]
        ).update(read_at=timezone.now())
        unread = selectors.unread_count(ctx(people.tech_mad))
        assert (unread.count, unread.more, unread.label) == (5, False, "5")

    def test_read_rows_are_not_counted(
        self, people: People, request_: MaterialRequest, note: Callable[..., InAppNotification]
    ) -> None:
        note(people.tech_mad, request_, read_at=timezone.now())
        assert selectors.unread_count(ctx(people.tech_mad)).label == "0"

    def test_the_list_is_newest_first_with_the_request_number(
        self, people: People, request_: MaterialRequest, note: Callable[..., InAppNotification]
    ) -> None:
        older = note(people.tech_mad, request_, Notice.REQUEST_APPROVED)
        newer = note(people.tech_mad, request_, Notice.REQUEST_HANDED_OVER)
        InAppNotification.objects.filter(pk=older.pk).update(notified_at=timezone.now() - DAY)
        found = list(selectors.in_app_notifications(ctx(people.tech_mad)))
        assert [row.pk for row in found] == [newer.pk, older.pk]
        assert {row.number for row in found} == {request_.number}  # type: ignore[attr-defined]
        assert found[0].spec.label == str(Notice.REQUEST_HANDED_OVER.label)

    def test_the_count_costs_one_query(
        self, people: People, request_: MaterialRequest, note: Callable[..., InAppNotification]
    ) -> None:
        note(people.tech_mad, request_)
        context = ctx(people.tech_mad)
        context.scope_for("request.view")  # the access context's own reads, once per request
        context.scope_for("request.review")
        with CaptureQueriesContext(connection) as queries:
            selectors.unread_count(context)
        assert len(queries) == 1


class TestOpen:
    def test_marks_it_read_and_leads_to_the_request(
        self, people: People, request_: MaterialRequest, note: Callable[..., InAppNotification]
    ) -> None:
        row = note(people.tech_mad, request_)
        path = open_notification(principal_for(people.tech_mad), row.pk)
        assert path == reverse("requests:detail", args=[request_.pk])
        row.refresh_from_db()
        assert row.read_at is not None

    def test_a_review_notice_leads_to_the_review_page(
        self, people: People, request_: MaterialRequest, note: Callable[..., InAppNotification]
    ) -> None:
        row = note(people.mm_mad, request_, Notice.REQUEST_TO_REVIEW)
        path = open_notification(principal_for(people.mm_mad), row.pk)
        assert path == reverse("requests:examine", args=[request_.pk])

    def test_opening_twice_keeps_the_first_read_time(
        self, people: People, request_: MaterialRequest, note: Callable[..., InAppNotification]
    ) -> None:
        read = timezone.now() - DAY
        row = note(people.tech_mad, request_, read_at=read)
        open_notification(principal_for(people.tech_mad), row.pk)
        row.refresh_from_db()
        assert row.read_at == read

    def test_another_persons_notification_is_not_found(
        self, people: People, request_: MaterialRequest, note: Callable[..., InAppNotification]
    ) -> None:
        theirs = note(people.mm_mad, request_)
        with pytest.raises(NotInScope):
            open_notification(principal_for(people.tech_mad), theirs.pk)
        theirs.refresh_from_db()
        assert theirs.read_at is None

    def test_one_about_a_request_out_of_scope_is_not_found(
        self, people: People, request_: MaterialRequest, note: Callable[..., InAppNotification]
    ) -> None:
        row = note(people.tech_mad, request_, Notice.REQUEST_TO_REVIEW)
        with pytest.raises(NotInScope):
            open_notification(principal_for(people.tech_mad), row.pk)

    def test_an_account_without_membership_is_refused(
        self, people: People, request_: MaterialRequest, note: Callable[..., InAppNotification]
    ) -> None:
        row = note(people.tech_mad, request_)
        Membership.objects.filter(user=people.tech_mad).update(revoked_at=timezone.now())
        with pytest.raises(AccessDenied):
            open_notification(principal_for(people.tech_mad), row.pk)

    def test_a_privileged_session_without_mfa_is_refused(
        self, people: People, request_: MaterialRequest, note: Callable[..., InAppNotification]
    ) -> None:
        row = note(people.mm_mad, request_, Notice.REQUEST_TO_REVIEW)
        with pytest.raises(MFARequired):
            open_notification(principal_for(people.mm_mad, mfa_at=None), row.pk)


class TestMarkAllRead:
    def test_marks_only_the_persons_visible_unread_rows(
        self,
        org: Org,
        people: People,
        request_: MaterialRequest,
        make_request: MakeRequest,
        note: Callable[..., InAppNotification],
    ) -> None:
        mine = [note(people.tech_mad, request_), note(people.tech_mad, request_)]
        hidden = note(people.tech_mad, request_, Notice.REQUEST_TO_REVIEW)
        theirs = note(people.mm_mad, request_)
        assert mark_all_read(principal_for(people.tech_mad)) == 2
        assert mark_all_read(principal_for(people.tech_mad)) == 0  # idempotent
        for row in mine:
            row.refresh_from_db()
            assert row.read_at is not None
        for row in (hidden, theirs):
            row.refresh_from_db()
            assert row.read_at is None

    @pytest.mark.parametrize("who", ["group_admin", "mm_mad", "engineer"])
    def test_a_privileged_role_needs_session_mfa(self, people: People, who: str) -> None:
        user = getattr(people, who)
        with pytest.raises(MFARequired):
            mark_all_read(principal_for(user, mfa_at=None))
        assert mark_all_read(principal_for(user)) == 0

    def test_every_other_role_may(self, people: People) -> None:
        for user in (people.tech_mad, people.site_manager, people.partner, people.wo_mad):
            assert mark_all_read(principal_for(user)) == 0

    @pytest.mark.parametrize("who", ["expired", "revoked", "deactivated", "nobody", "operator"])
    def test_without_a_current_membership_it_is_refused(self, people: People, who: str) -> None:
        with pytest.raises(AccessDenied):
            mark_all_read(principal_for(getattr(people, who)))

    def test_an_offboarded_account_is_refused(self, people: People) -> None:
        User.objects.filter(pk=people.tech_mad.pk).update(offboarded_at=timezone.now())
        with pytest.raises(AccessDenied):
            mark_all_read(principal_for(people.tech_mad))


# --- the purge (Q98) ---------------------------------------------------------------------------


@pytest.fixture
def aged(people: People, request_: MaterialRequest) -> Callable[..., list[InAppNotification]]:
    """``count`` rows of a person, notified ``days`` ago, read ``read_days`` ago (or unread)."""

    def make(
        user: User, count: int = 1, *, days: float = 0, read_days: float | None = None
    ) -> list[InAppNotification]:
        now = timezone.now()
        return InAppNotification.objects.bulk_create(
            InAppNotification(
                user=user,
                notice=Notice.REQUEST_HANDED_OVER,
                object_type=SUBJECT,
                object_id=request_.pk,
                notified_at=now - timedelta(days=days, seconds=index),
                read_at=None if read_days is None else now - timedelta(days=read_days),
            )
            for index in range(count)
        )

    return make


def left(user: User) -> int:
    return InAppNotification.objects.filter(user=user).count()


class TestPurge:
    def test_read_rows_go_after_30_days(self, people: People, aged: Callable[..., Any]) -> None:
        aged(people.tech_mad, days=40, read_days=31)
        kept = aged(people.tech_mad, days=40, read_days=29)
        assert housekeeping.purge_in_app_notifications() == 1
        assert list(InAppNotification.objects.all()) == kept

    def test_unread_rows_go_after_90_days(self, people: People, aged: Callable[..., Any]) -> None:
        aged(people.tech_mad, days=91)
        kept = aged(people.tech_mad, days=89)
        assert housekeeping.purge_in_app_notifications() == 1
        assert list(InAppNotification.objects.all()) == kept

    def test_beyond_the_newest_200_of_a_person(
        self, people: People, aged: Callable[..., Any]
    ) -> None:
        newest = aged(people.tech_mad, 150, days=1)
        aged(people.tech_mad, 60, days=2, read_days=1)  # older, read or not
        aged(people.mm_mad, 150, days=3)
        assert housekeeping.purge_in_app_notifications() == 10
        assert left(people.tech_mad) == 200 and left(people.mm_mad) == 150
        assert set(newest) <= set(InAppNotification.objects.filter(user=people.tech_mad))

    def test_the_cap_comes_from_the_settings(
        self, people: People, aged: Callable[..., Any], settings: Any
    ) -> None:
        settings.IN_APP_MAX_PER_PERSON = 10
        settings.IN_APP_UNREAD_RETENTION = 5
        aged(people.tech_mad, 12)
        aged(people.mm_mad, 2, days=6)
        assert housekeeping.purge_in_app_notifications() == 4
        assert left(people.tech_mad) == 10 and left(people.mm_mad) == 0

    def test_the_cap_counts_rows_notified_at_the_same_time(
        self, people: People, request_: MaterialRequest, settings: Any
    ) -> None:
        """Rows written by one transaction share their time: the id decides which go."""
        settings.IN_APP_MAX_PER_PERSON = 10
        rows = InAppNotification.objects.bulk_create(
            InAppNotification(
                user=people.tech_mad,
                notice=Notice.REQUEST_HANDED_OVER,
                object_type=SUBJECT,
                object_id=request_.pk,
            )
            for _ in range(12)
        )
        assert len({row.notified_at for row in InAppNotification.objects.all()}) == 1
        assert housekeeping.purge_in_app_notifications() == 2
        kept = set(InAppNotification.objects.values_list("pk", flat=True))
        assert kept == set(sorted(row.pk for row in rows)[2:])  # the two lowest ids went

    def test_each_batch_is_a_step(
        self, people: People, aged: Callable[..., Any], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(housekeeping, "PURGE_BATCH", 4)
        aged(people.tech_mad, 10, days=100)
        steps: list[int] = []

        def run_step(step: Callable[[], int]) -> int:
            steps.append(done := step())
            return done

        assert housekeeping.purge_in_app_notifications(run_step) == 10
        assert steps == [0, 0, 4, 4, 2, 0]  # offboarded, read, unread in three, over the cap

    def test_an_offboarded_persons_rows_go_after_30_days(
        self, people: People, aged: Callable[..., Any]
    ) -> None:
        aged(people.tech_mad, 3)
        aged(people.mm_mad, 2)
        User.objects.filter(pk=people.tech_mad.pk).update(offboarded_at=timezone.now() - 31 * DAY)
        User.objects.filter(pk=people.mm_mad.pk).update(offboarded_at=timezone.now() - 29 * DAY)
        assert housekeeping.purge_in_app_notifications() == 3
        assert left(people.tech_mad) == 0 and left(people.mm_mad) == 2

    def test_in_batches(
        self, people: People, aged: Callable[..., Any], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(housekeeping, "PURGE_BATCH", 4)
        aged(people.tech_mad, 10, days=100)
        with CaptureQueriesContext(connection) as queries:
            assert housekeeping.purge_in_app_notifications() == 10
        deletes = [q["sql"] for q in queries if q["sql"].startswith("DELETE")]
        # 4 + 4 + 2 for the unread rule (a short batch ends it), one empty batch for each other
        assert len(deletes) == 3 + 3
        assert all("LIMIT 4" in sql for sql in deletes)

    def test_stops_when_the_budget_is_spent(
        self, people: People, aged: Callable[..., Any], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(housekeeping, "PURGE_BATCH", 2)
        aged(people.tech_mad, 10, days=100)
        # The deadline, the empty offboarded and read rules, two unread batches, then spent.
        ticks: Iterator[float] = iter([0.0, 0.0, 0.0, 0.0, 1.0, housekeeping.PURGE_BUDGET, 99.0])
        assert housekeeping.purge_in_app_notifications(clock=lambda: next(ticks)) == 4
        assert left(people.tech_mad) == 6  # the next run goes on

    def test_logs_the_counts_and_nobody(
        self, people: People, aged: Callable[..., Any], caplog: pytest.LogCaptureFixture
    ) -> None:
        aged(people.tech_mad, days=91)
        with caplog.at_level(logging.INFO, logger=housekeeping.__name__):
            housekeeping.purge_in_app_notifications()
        (line,) = [r.getMessage() for r in caplog.records if "In-app" in r.getMessage()]
        assert "unread=1" in line and "read=0" in line
        assert str(people.tech_mad.pk) not in line and people.tech_mad.email not in line

    def test_runs_with_the_workers_housekeeping(
        self, people: People, aged: Callable[..., Any]
    ) -> None:
        aged(people.tech_mad, days=91)
        done = run_housekeeping(SystemPrincipal())
        assert done[housekeeping.IN_APP_TASK] == 1
        assert left(people.tech_mad) == 0

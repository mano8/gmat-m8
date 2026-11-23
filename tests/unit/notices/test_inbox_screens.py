"""The bell and the list of in-app notifications (M17.4, Q94, Q99).

The bell's count is drawn with each page and reloaded by a GET-only, session-passive fragment;
the list pages in the database; opening one and marking all are POSTs on the person's own rows,
and another person's notification is a 404. Nothing here writes on a GET or a HEAD.
"""

import re
from collections.abc import Callable
from datetime import timedelta
from typing import Any

import pytest
from django.test import Client
from django.urls import reverse
from django.utils import timezone, translation

from apps.material_requests.models import MaterialRequest
from apps.notifications.kinds import SUBJECT, Notice
from apps.notifications.models import InAppNotification
from apps.organizations.models import Membership
from tests.org import People
from tests.org import client_for as signed_in

pytestmark = pytest.mark.django_db

DAY = timedelta(days=1)


@pytest.fixture
def request_(make_request: Callable[..., MaterialRequest]) -> MaterialRequest:
    return make_request(MaterialRequest.Status.SUBMITTED)


@pytest.fixture
def note(request_: MaterialRequest) -> Callable[..., InAppNotification]:
    def make(
        user: Any, notice: Notice = Notice.REQUEST_APPROVED, **extra: Any
    ) -> InAppNotification:
        return InAppNotification.objects.create(
            user=user, notice=notice, object_type=SUBJECT, object_id=request_.pk, **extra
        )

    return make


def client_for(user: Any) -> Client:
    return signed_in(user, HTTP_ACCEPT_LANGUAGE="en")


def unread(row: InAppNotification) -> bool:
    row.refresh_from_db()
    return row.read_at is None


class TestTheBell:
    def test_a_page_carries_it_with_the_count_in_its_accessible_name(
        self, people: People, note: Callable[..., InAppNotification]
    ) -> None:
        note(people.tech_mad)
        note(people.tech_mad, Notice.REQUEST_REJECTED)
        html = client_for(people.tech_mad).get(reverse("core:home")).content.decode()
        bell = re.search(r'<a id="notification-bell".*?</a>', html, re.S)
        assert bell is not None
        assert 'aria-label="Notifications, 2 unread"' in bell.group(0)
        assert f'href="{reverse("inapp:list")}"' in bell.group(0)
        assert 'data-unread="2"' in bell.group(0) and "data-poll" in bell.group(0)
        assert 'hx-trigger="every 60s"' in bell.group(0)
        assert "aria-live" not in bell.group(0)

    def test_without_unread_it_is_a_plain_link(self, people: People) -> None:
        html = client_for(people.tech_mad).get(reverse("core:home")).content.decode()
        assert (
            'aria-label="Notifications"' in html and 'data-testid="notification-count"' not in html
        )

    def test_who_cannot_read_notifications_has_no_bell(self, people: People) -> None:
        html = client_for(people.nobody).get(reverse("sessions:list")).content.decode()
        assert 'id="notification-bell"' not in html

    def test_the_fragment_is_the_count_alone_and_polls_again(
        self, people: People, note: Callable[..., InAppNotification]
    ) -> None:
        note(people.tech_mad)
        response = client_for(people.tech_mad).get(reverse("inapp:bell"))
        assert response.status_code == 200
        body = response.content.decode()
        assert body.lstrip().startswith('<a id="notification-bell"') and "<html" not in body
        assert 'aria-label="Notifications, 1 unread"' in body

    def test_it_stops_at_99_plus(
        self, people: People, note: Callable[..., InAppNotification]
    ) -> None:
        rows = [note(people.tech_mad) for _ in range(100)]
        assert len(rows) == 100
        body = client_for(people.tech_mad).get(reverse("inapp:bell")).content.decode()
        assert "99+" in body and 'data-unread="99"' in body

    def test_the_poll_is_not_activity_and_writes_nothing(
        self, people: People, note: Callable[..., InAppNotification]
    ) -> None:
        from apps.notifications import inbox_views

        assert getattr(inbox_views.bell, "session_passive", False) is True
        row = note(people.tech_mad)
        client = client_for(people.tech_mad)
        assert client.get(reverse("inapp:bell")).status_code == 200
        assert client.head(reverse("inapp:bell")).status_code == 200
        assert client.post(reverse("inapp:bell")).status_code == 405
        assert unread(row)

    def test_it_counts_only_what_the_person_may_still_see(
        self, people: People, note: Callable[..., InAppNotification]
    ) -> None:
        note(people.tech_mad, Notice.REQUEST_TO_REVIEW)  # a technician cannot review
        note(people.mm_mad)
        body = client_for(people.tech_mad).get(reverse("inapp:bell")).content.decode()
        assert 'data-unread="0"' in body

    def test_a_member_without_a_membership_is_refused_the_fragment(self, people: People) -> None:
        assert client_for(people.nobody).get(reverse("inapp:bell")).status_code == 403
        assert Client().get(reverse("inapp:bell")).status_code == 302


class TestTheList:
    def test_newest_first_with_the_number_the_label_and_the_state(
        self, people: People, request_: MaterialRequest, note: Callable[..., InAppNotification]
    ) -> None:
        older = note(people.tech_mad, read_at=timezone.now())
        newer = note(people.tech_mad, Notice.REQUEST_HANDED_OVER)
        InAppNotification.objects.filter(pk=older.pk).update(notified_at=timezone.now() - DAY)
        html = client_for(people.tech_mad).get(reverse("inapp:list")).content.decode()
        assert html.index(reverse("inapp:open", args=[newer.pk])) < html.index(
            reverse("inapp:open", args=[older.pk])
        )
        assert request_.number and request_.number in html
        assert html.count('data-state="unread"') == 1 and html.count('data-state="read"') == 1
        with translation.override("en"):
            assert str(Notice.REQUEST_HANDED_OVER.label) in html
        assert 'data-testid="read-all-form"' in html

    def test_nothing_unread_offers_no_mark_all(
        self, people: People, note: Callable[..., InAppNotification]
    ) -> None:
        note(people.tech_mad, read_at=timezone.now())
        html = client_for(people.tech_mad).get(reverse("inapp:list")).content.decode()
        assert 'data-testid="read-all-form"' not in html

    def test_an_empty_list_says_so(self, people: People) -> None:
        html = client_for(people.tech_mad).get(reverse("inapp:list")).content.decode()
        assert "No notifications" in html and 'data-testid="notification-list"' not in html

    def test_only_the_persons_own_rows_that_they_may_still_see(
        self, people: People, note: Callable[..., InAppNotification]
    ) -> None:
        mine = note(people.tech_mad)
        theirs = note(people.mm_mad)
        hidden = note(people.tech_mad, Notice.REQUEST_TO_REVIEW)
        html = client_for(people.tech_mad).get(reverse("inapp:list")).content.decode()
        assert str(mine.pk) in html
        assert str(theirs.pk) not in html and str(hidden.pk) not in html

    def test_lost_access_hides_what_was_delivered(
        self, people: People, note: Callable[..., InAppNotification]
    ) -> None:
        row = note(people.tech_mad)
        Membership.objects.filter(user=people.tech_mad).update(revoked_at=timezone.now())
        assert client_for(people.tech_mad).get(reverse("inapp:list")).status_code == 403
        assert unread(row)

    def test_pages_in_the_database(
        self, people: People, note: Callable[..., InAppNotification]
    ) -> None:
        for _ in range(30):
            note(people.tech_mad)
        client = client_for(people.tech_mad)
        first = client.get(reverse("inapp:list")).content.decode()
        second = client.get(reverse("inapp:list"), {"page": 2}).content.decode()
        assert first.count('data-testid="notification"') == 25
        assert second.count('data-testid="notification"') == 5
        assert "per_page" not in first  # the size is fixed

    def test_a_get_and_a_head_write_nothing(
        self, people: People, note: Callable[..., InAppNotification]
    ) -> None:
        row = note(people.tech_mad)
        client = client_for(people.tech_mad)
        assert client.get(reverse("inapp:list")).status_code == 200
        assert client.head(reverse("inapp:list")).status_code == 200
        assert client.post(reverse("inapp:list")).status_code == 405
        assert unread(row)

    def test_the_htmx_variant_is_the_results_alone(
        self, people: People, note: Callable[..., InAppNotification]
    ) -> None:
        note(people.tech_mad)
        response = client_for(people.tech_mad).get(
            reverse("inapp:list"), headers={"HX-Request": "true"}
        )
        body = response.content.decode()
        assert response.status_code == 200 and "<html" not in body
        assert 'id="notification-results"' in body and "HX-Request" in response["Vary"]


class TestOpening:
    def test_a_post_marks_it_read_and_leads_to_the_request(
        self, people: People, request_: MaterialRequest, note: Callable[..., InAppNotification]
    ) -> None:
        row = note(people.tech_mad)
        client = client_for(people.tech_mad)
        assert client.get(reverse("inapp:open", args=[row.pk])).status_code == 405
        assert unread(row)
        response = client.post(reverse("inapp:open", args=[row.pk]))
        assert response.status_code == 302
        assert response["Location"] == reverse("requests:detail", args=[request_.pk])
        assert not unread(row)

    def test_another_persons_is_a_404_and_stays_unread(
        self, people: People, note: Callable[..., InAppNotification]
    ) -> None:
        theirs = note(people.mm_mad)
        assert (
            client_for(people.tech_mad).post(reverse("inapp:open", args=[theirs.pk])).status_code
            == 404
        )
        assert unread(theirs)

    def test_one_that_left_the_persons_scope_is_a_404(
        self, people: People, note: Callable[..., InAppNotification]
    ) -> None:
        row = note(people.tech_mad, Notice.REQUEST_TO_REVIEW)
        assert (
            client_for(people.tech_mad).post(reverse("inapp:open", args=[row.pk])).status_code
            == 404
        )
        assert unread(row)

    def test_an_unknown_id_is_a_404_and_a_stranger_is_refused(self, people: People) -> None:
        unknown = reverse("inapp:open", args=["00000000-0000-7000-8000-000000000000"])
        assert client_for(people.tech_mad).post(unknown).status_code == 404
        assert client_for(people.nobody).post(unknown).status_code == 403


class TestMarkingAll:
    def test_marks_the_persons_visible_rows_and_says_how_many(
        self, people: People, note: Callable[..., InAppNotification]
    ) -> None:
        mine = [note(people.tech_mad), note(people.tech_mad)]
        theirs = note(people.mm_mad)
        client = client_for(people.tech_mad)
        assert client.get(reverse("inapp:read_all")).status_code == 405
        response = client.post(reverse("inapp:read_all"), follow=True)
        assert response.redirect_chain[-1][0] == reverse("inapp:list")
        assert "2 notifications marked as read." in response.content.decode()
        assert all(not unread(row) for row in mine) and unread(theirs)

    def test_with_nothing_unread_it_says_so(self, people: People) -> None:
        response = client_for(people.tech_mad).post(reverse("inapp:read_all"), follow=True)
        assert "Nothing to mark as read." in response.content.decode()

    def test_a_stranger_is_refused(self, people: People) -> None:
        assert client_for(people.nobody).post(reverse("inapp:read_all")).status_code == 403

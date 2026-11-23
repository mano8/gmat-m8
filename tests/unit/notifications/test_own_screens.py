"""*My notifications* (M17.4, Q93, Q97): where the person is told, what they get there, and the
dialogs that change each part from their own page.

Every member changes their own choices and nobody else's; what the administrators set is shown
beside them, and the person's own choice wins over it.
"""

import re

import pytest
from django.contrib.messages import get_messages
from django.test import Client
from django.urls import reverse

from apps.audit.models import AuditEvent
from apps.core.idempotency import issue_key
from apps.notifications import resolution
from apps.notifications.kinds import NOTICES, Channel, Mode, Notice, channel_slug
from apps.notifications.models import (
    ChannelSwitch,
    NoticeChoice,
    NoticeDefault,
    NoticeSetting,
    NotificationPreference,
)
from apps.notifications.services import OWN_CHANNELS_OPERATION, OWN_NOTICES_OPERATION
from tests.org import People
from tests.org import client_for as signed_in

pytestmark = pytest.mark.django_db

HTMX = {"HX-Request": "true"}
PAGE = "notifications:settings"


def client_for(user: object) -> Client:
    return signed_in(user, HTTP_ACCEPT_LANGUAGE="en")  # type: ignore[arg-type]


def choices_url(channel: Channel) -> str:
    return reverse("notifications:save_choices", args=[channel_slug(channel)])


def html_of(client: Client) -> str:
    response = client.get(reverse(PAGE))
    assert response.status_code == 200
    return response.content.decode()


def key_in(html: str, field: str) -> str:
    found = re.search(rf'id="{field}" value="([^"]+)"', html) or re.search(
        rf'value="([^"]+)"[^>]*id="{field}"', html
    )
    assert found is not None, field
    return found.group(1)


def text_of(html: str, pattern: str) -> str:
    found = re.search(pattern, html, re.S)
    assert found is not None, pattern
    return " ".join(re.sub(r"<[^>]+>", " ", found.group(0)).split())


def cell(html: str, notice: Notice, channel: Channel) -> str:
    row = re.search(rf'data-notice="{notice.value}".*?</tr>', html, re.S)
    assert row is not None
    return text_of(row.group(0), rf'<td[^>]*data-channel="{channel.value}".*?</td>')


class TestThePage:
    def test_where_then_what_with_a_dialog_for_each(self, people: People) -> None:
        html = html_of(client_for(people.tech_mad))
        assert html.index('data-testid="notification-settings"') < html.index(
            'data-testid="own-notices"'
        )
        assert 'data-testid="channels-open"' in html
        for channel in Channel:
            slug = channel_slug(channel)
            assert f'data-testid="choices-{slug}-open"' in html
            assert f'data-testid="choices-{slug}-form"' in html
        assert 'data-testid="channels-form"' in html
        rows = re.findall(r'data-testid="notice-state"', html)
        assert len(rows) == len(NOTICES)

    def test_every_channel_is_on_at_first_and_every_notice_follows_everyone(
        self, people: People
    ) -> None:
        html = html_of(client_for(people.tech_mad))
        assert text_of(html, r'data-testid="channels".*?</ul>').count("On") == len(Channel)
        text = cell(html, Notice.REQUEST_APPROVED, Channel.IN_APP)
        assert "Sent" in text and "Follows the setting for everyone." in text

    def test_what_an_administrator_set_is_shown_and_the_roles_that_never_receive(
        self, people: People
    ) -> None:
        tech = people.tech_mad
        NoticeDefault.objects.create(
            notice=Notice.REQUEST_REJECTED,
            channel=Channel.IN_APP,
            enabled=False,
            changed_by=people.group_admin,
        )
        NoticeSetting.objects.create(
            user=tech,
            notice=Notice.REQUEST_APPROVED,
            channel=Channel.EMAIL,
            mode=Mode.NEVER,
            set_by=people.group_admin,
        )
        html = html_of(client_for(tech))
        approved = cell(html, Notice.REQUEST_APPROVED, Channel.EMAIL)
        assert "An administrator set it never to reach you." in approved and "Not sent" in approved
        rejected = cell(html, Notice.REQUEST_REJECTED, Channel.IN_APP)
        assert "Follows the setting for everyone." in rejected and "Not sent" in rejected
        review = text_of(html, rf'data-notice="{Notice.REQUEST_TO_REVIEW.value}".*?</th>')
        assert "Your current roles do not receive it." in review

    def test_a_channel_turned_off_greys_its_column_and_says_so(self, people: People) -> None:
        NotificationPreference.objects.create(user=people.mm_mad, email_opt_out=True)
        html = html_of(client_for(people.mm_mad))
        text = cell(html, Notice.REQUEST_TO_REVIEW, Channel.EMAIL)
        assert "You turned this channel off." in text and "Not sent" in text
        assert 'class="opacity-60"' in html
        assert "Sent" in cell(html, Notice.REQUEST_TO_REVIEW, Channel.IN_APP)

    def test_their_own_choice_says_where_it_overrides_the_administrators(
        self, people: People
    ) -> None:
        mm = people.mm_mad
        NoticeSetting.objects.create(
            user=mm,
            notice=Notice.REQUEST_TO_REVIEW,
            channel=Channel.EMAIL,
            mode=Mode.NEVER,
            set_by=people.group_admin,
        )
        NoticeChoice.objects.create(
            user=mm, notice=Notice.REQUEST_TO_REVIEW, channel=Channel.EMAIL, enabled=True
        )
        text = cell(html_of(client_for(mm)), Notice.REQUEST_TO_REVIEW, Channel.EMAIL)
        assert "You chose this yourself." in text
        assert "It overrides what the administrators set." in text and "Not sent" not in text

    def test_the_dialog_says_what_following_the_administrators_gives(self, people: People) -> None:
        NoticeDefault.objects.create(
            notice=Notice.REQUEST_REJECTED,
            channel=Channel.EMAIL,
            enabled=False,
            changed_by=people.group_admin,
        )
        html = html_of(client_for(people.tech_mad))
        assert "As the administrators set it: not sent" in html
        assert "As the administrators set it: sent" in html

    def test_nothing_of_another_person_reaches_the_page(self, people: People) -> None:
        NoticeSetting.objects.create(
            user=people.mm_mad,
            notice=Notice.REQUEST_TO_REVIEW,
            channel=Channel.EMAIL,
            mode=Mode.NEVER,
            set_by=people.group_admin,
        )
        html = html_of(client_for(people.tech_mad))
        assert people.mm_mad.email not in html
        assert "never to reach you" not in cell(html, Notice.REQUEST_TO_REVIEW, Channel.EMAIL)

    def test_a_person_without_a_membership_is_refused(self, people: People) -> None:
        assert client_for(people.nobody).get(reverse(PAGE)).status_code == 403


class TestWhere:
    def test_turning_a_channel_off_and_on(self, people: People) -> None:
        tech = people.tech_mad
        client = client_for(tech)
        html = html_of(client)
        data = {
            "idempotency_key": key_in(html, "id_channels_idempotency_key"),
            "seen_EMAIL": "on",
            "seen_IN_APP": "on",
            "IN_APP": "on",  # the email box is unticked: off
        }
        response = client.post(reverse("notifications:save_channels"), data, headers=HTMX)
        assert response.status_code == 204 and response["HX-Redirect"] == reverse(PAGE)
        assert NotificationPreference.objects.get(user=tech).email_opt_out is True
        assert not ChannelSwitch.objects.exists()  # in-app was left as it showed
        assert resolution.wanted([tech.pk], Notice.REQUEST_APPROVED) == set()
        assert resolution.wanted([tech.pk], Notice.REQUEST_APPROVED, Channel.IN_APP) == {tech.pk}
        assert AuditEvent.objects.filter(action="notification.own_changed").count() == 1
        again = html_of(client)
        assert "You turned this channel off." in cell(again, Notice.REQUEST_APPROVED, Channel.EMAIL)
        # In the application off, email back on.
        data = {
            "idempotency_key": issue_key(tech.pk, OWN_CHANNELS_OPERATION),
            "seen_EMAIL": "off",
            "seen_IN_APP": "on",
            "EMAIL": "on",
        }
        assert client.post(reverse("notifications:save_channels"), data).status_code == 302
        assert NotificationPreference.objects.get(user=tech).email_opt_out is False
        assert ChannelSwitch.objects.get(user=tech).enabled is False

    def test_a_post_without_what_was_shown_names_every_channel(self, people: People) -> None:
        client = client_for(people.tech_mad)
        data = {"idempotency_key": issue_key(people.tech_mad.pk, OWN_CHANNELS_OPERATION)}
        assert client.post(reverse("notifications:save_channels"), data).status_code == 302
        assert NotificationPreference.objects.get().email_opt_out is True
        assert ChannelSwitch.objects.get().enabled is False

    def test_a_save_that_changes_nothing_writes_nothing(self, people: People) -> None:
        client = client_for(people.tech_mad)
        data = {
            "idempotency_key": issue_key(people.tech_mad.pk, OWN_CHANNELS_OPERATION),
            "seen_EMAIL": "on",
            "seen_IN_APP": "on",
            "EMAIL": "on",
            "IN_APP": "on",
        }
        client.post(reverse("notifications:save_channels"), data)
        assert not NotificationPreference.objects.exists() and not ChannelSwitch.objects.exists()
        assert not AuditEvent.objects.filter(action="notification.own_changed").exists()

    def test_a_double_tap_changes_once(self, people: People) -> None:
        client = client_for(people.tech_mad)
        data = {
            "idempotency_key": issue_key(people.tech_mad.pk, OWN_CHANNELS_OPERATION),
            "seen_EMAIL": "on",
            "seen_IN_APP": "on",
        }
        for _ in range(2):
            assert client.post(reverse("notifications:save_channels"), data).status_code == 302
        assert AuditEvent.objects.filter(action="notification.own_changed").count() == 1

    def test_an_invalid_post_comes_back_in_the_dialog(self, people: People) -> None:
        client = client_for(people.tech_mad)
        data = {"seen_EMAIL": "maybe"}
        response = client.post(reverse("notifications:save_channels"), data, headers=HTMX)
        assert response.status_code == 422
        assert b'data-testid="channels-form"' in response.content
        assert not NotificationPreference.objects.exists()

    def test_only_a_post_and_only_a_member(self, people: People) -> None:
        url = reverse("notifications:save_channels")
        assert client_for(people.tech_mad).get(url).status_code == 405
        assert client_for(people.nobody).post(url, {}).status_code == 403
        assert Client().post(url, {}).status_code == 302


class TestWhat:
    def data(self, html: str, channel: Channel, **choices: str) -> dict[str, str]:
        slug = channel_slug(channel).replace("-", "_")
        data = {"idempotency_key": key_in(html, f"id_choices_{slug}_idempotency_key")}
        data |= {f"seen_{notice.value}": "" for notice in Notice}
        return data | choices

    def test_a_choice_wins_over_what_the_administrators_set(self, people: People) -> None:
        tech = people.tech_mad
        NoticeSetting.objects.create(
            user=tech,
            notice=Notice.REQUEST_APPROVED,
            channel=Channel.EMAIL,
            mode=Mode.NEVER,
            set_by=people.group_admin,
        )
        client = client_for(tech)
        data = self.data(html_of(client), Channel.EMAIL, REQUEST_APPROVED="on")
        response = client.post(choices_url(Channel.EMAIL), data, headers=HTMX)
        assert response.status_code == 204 and response["HX-Redirect"] == reverse(PAGE)
        choice = NoticeChoice.objects.get(user=tech)
        assert (choice.notice, choice.channel, choice.enabled) == (
            Notice.REQUEST_APPROVED,
            Channel.EMAIL,
            True,
        )
        assert resolution.wanted([tech.pk], Notice.REQUEST_APPROVED) == {tech.pk}
        # The dialog now starts from the choice and the cell says whose it is.
        again = html_of(client)
        assert re.search(r'<option value="on" selected>', again)
        assert "It overrides what the administrators set." in cell(
            again, Notice.REQUEST_APPROVED, Channel.EMAIL
        )

    def test_a_choice_to_refuse_and_a_return_to_the_administrators(self, people: People) -> None:
        tech = people.tech_mad
        client = client_for(tech)
        data = self.data(html_of(client), Channel.IN_APP, REQUEST_APPROVED="off")
        client.post(choices_url(Channel.IN_APP), data)
        assert NoticeChoice.objects.get().enabled is False
        assert resolution.wanted([tech.pk], Notice.REQUEST_APPROVED, Channel.IN_APP) == set()
        assert resolution.wanted([tech.pk], Notice.REQUEST_APPROVED) == {tech.pk}
        # Back to "as the administrators set it": the row goes.
        back = {
            "idempotency_key": issue_key(tech.pk, OWN_NOTICES_OPERATION),
            "REQUEST_APPROVED": "",
            "seen_REQUEST_APPROVED": "off",
        }
        client.post(choices_url(Channel.IN_APP), back)
        assert not NoticeChoice.objects.exists()
        assert AuditEvent.objects.filter(action="notification.own_changed").count() == 2

    def test_a_save_changes_only_what_the_dialog_changed(self, people: People) -> None:
        """A change made in another tab since the dialog was drawn is not undone."""
        tech = people.tech_mad
        client = client_for(tech)
        html = html_of(client)
        NoticeChoice.objects.create(
            user=tech, notice=Notice.REQUEST_APPROVED, channel=Channel.EMAIL, enabled=False
        )
        data = self.data(html, Channel.EMAIL, REQUEST_APPROVED="", REQUEST_EDITED="on")
        client.post(choices_url(Channel.EMAIL), data)
        assert {(row.notice, row.enabled) for row in NoticeChoice.objects.all()} == {
            (Notice.REQUEST_APPROVED, False),
            (Notice.REQUEST_EDITED, True),
        }

    def test_a_post_without_what_was_shown_names_every_notice(self, people: People) -> None:
        client = client_for(people.tech_mad)
        data = {"idempotency_key": issue_key(people.tech_mad.pk, OWN_NOTICES_OPERATION)}
        data |= {notice.value: "off" for notice in Notice}
        assert client.post(choices_url(Channel.EMAIL), data).status_code == 302
        assert NoticeChoice.objects.count() == len(Notice)

    def test_the_other_channel_is_left_alone(self, people: People) -> None:
        client = client_for(people.tech_mad)
        data = self.data(html_of(client), Channel.EMAIL, REQUEST_EDITED="off")
        client.post(choices_url(Channel.EMAIL), data)
        assert {row.channel for row in NoticeChoice.objects.all()} == {Channel.EMAIL}

    def test_nothing_changed_is_still_a_save(self, people: People) -> None:
        client = client_for(people.tech_mad)
        response = client.post(
            choices_url(Channel.EMAIL), self.data(html_of(client), Channel.EMAIL)
        )
        assert response.status_code == 302
        assert [str(m) for m in get_messages(response.wsgi_request)] == [
            "Your notification settings were saved."
        ]
        assert not NoticeChoice.objects.exists()

    def test_an_unknown_choice_comes_back_in_the_dialog(self, people: People) -> None:
        client = client_for(people.tech_mad)
        data = self.data(html_of(client), Channel.EMAIL, REQUEST_APPROVED="sometimes")
        response = client.post(choices_url(Channel.EMAIL), data, headers=HTMX)
        assert response.status_code == 422
        assert b'data-testid="choices-email-form"' in response.content
        assert b"As the administrators set it" in response.content
        assert not NoticeChoice.objects.exists()

    def test_an_unknown_channel_a_get_and_a_stranger(self, people: People) -> None:
        unknown = reverse("notifications:save_choices", args=["telegram"])
        assert client_for(people.tech_mad).post(unknown, {}).status_code == 404
        assert client_for(people.tech_mad).get(choices_url(Channel.EMAIL)).status_code == 405
        assert client_for(people.nobody).post(choices_url(Channel.EMAIL), {}).status_code == 403

    def test_only_the_signed_in_persons_rows_change(self, people: People) -> None:
        client = client_for(people.tech_mad)
        data = self.data(html_of(client), Channel.EMAIL, REQUEST_EDITED="off")
        data["user_id"] = str(people.mm_mad.pk)  # a field the form does not have
        client.post(choices_url(Channel.EMAIL), data)
        assert {row.user_id for row in NoticeChoice.objects.all()} == {people.tech_mad.pk}

"""Which business notifications are sent: the administration page and the person's page, per
channel (M15.3, M17.4: Q87-Q89, Q93, Q97).

A group administrator sets the notifications for everyone and for a person from dialogs; the
person's own choices (their page, ``tests/unit/notifications/test_own_screens.py``) are shown
read-only beside them. Nobody else reads another person's settings.
"""

import re

import pytest
from django.contrib.messages import get_messages
from django.test import Client
from django.urls import reverse
from django.utils import translation
from django.utils.html import escape

from apps.accounts.models import User
from apps.audit.models import AuditEvent
from apps.core.idempotency import issue_key
from apps.notifications import resolution, services
from apps.notifications.kinds import NOTICES, Channel, Mode, Notice, channel_slug
from apps.notifications.models import (
    ChannelSwitch,
    NoticeChoice,
    NoticeDefault,
    NoticeSetting,
    NotificationPreference,
)
from apps.notifications.services import NOTICE_CHANNELS_OPERATION, PERSON_NOTICES_OPERATION
from tests.org import People
from tests.org import client_for as signed_in
from tests.support import principal_for

pytestmark = pytest.mark.django_db

HTMX = {"HX-Request": "true"}


def client_for(user: User) -> Client:
    """Signed in, asking for English: the texts these tests read."""
    return signed_in(user, HTTP_ACCEPT_LANGUAGE="en")


def default_url(notice: Notice) -> str:
    return reverse("org:notification_default", args=[notice.value])


def person_url(user: User, channel: Channel = Channel.EMAIL) -> str:
    return reverse("org:person_notifications", args=[user.pk, channel_slug(channel)])


def html_of(client: Client, url: str) -> str:
    response = client.get(url)
    assert response.status_code == 200
    return response.content.decode()


def key_of(html: str, field: str) -> str:
    found = re.search(rf'id="{field}" value="([^"]+)"', html) or re.search(
        rf'value="([^"]+)"[^>]*id="{field}"', html
    )
    assert found is not None, field
    return found.group(1)


def state_of(html: str, notice: Notice) -> str:
    """The text of one notice's row of the page: its state and what is said about it."""
    row = re.search(rf'data-notice="{notice.value}".*?</tr>', html, re.S)
    assert row is not None, notice
    return " ".join(re.sub(r"<[^>]+>", " ", row.group(0)).split())


def cell_of(html: str, notice: Notice, channel: Channel) -> str:
    """The text of one cell of a notice's row: its state on one channel and why."""
    row = re.search(rf'data-notice="{notice.value}".*?</tr>', html, re.S)
    assert row is not None, notice
    cell = re.search(rf'<td[^>]*data-channel="{channel.value}".*?</td>', row.group(0), re.S)
    assert cell is not None, (notice, channel)
    return " ".join(re.sub(r"<[^>]+>", " ", cell.group(0)).split())


class TestAdministrationPage:
    def test_every_notification_in_the_registry_order_with_who_and_when(
        self, people: People
    ) -> None:
        html = html_of(client_for(people.group_admin), reverse("org:notifications"))
        positions = [html.index(f'data-notice="{notice.value}"') for notice in NOTICES]
        assert positions == sorted(positions)
        with translation.override("en"):
            for spec in NOTICES.values():
                assert escape(str(spec.recipients)) in html and escape(str(spec.when)) in html

    def test_all_on_at_first_on_every_channel_and_no_own_settings(self, people: People) -> None:
        html = html_of(client_for(people.group_admin), reverse("org:notifications"))
        for notice in NOTICES:
            for channel in Channel:
                text = cell_of(html, notice, channel)
                assert "Not sent" not in text and "Sent" in text
                assert "No settings of their own" in text

    def test_a_notification_off_on_one_channel_only(self, people: People) -> None:
        admin = people.group_admin
        NoticeDefault.objects.create(
            notice=Notice.REQUEST_APPROVED, channel=Channel.IN_APP, enabled=False, changed_by=admin
        )
        for user, mode in ((people.tech_mad, Mode.ALWAYS), (people.mm_mad, Mode.NEVER)):
            NoticeSetting.objects.create(
                user=user,
                notice=Notice.REQUEST_APPROVED,
                channel=Channel.EMAIL,
                mode=mode,
                set_by=admin,
            )
        html = html_of(client_for(admin), reverse("org:notifications"))
        in_app = cell_of(html, Notice.REQUEST_APPROVED, Channel.IN_APP)
        assert "Not sent" in in_app and "No settings of their own" in in_app
        email = cell_of(html, Notice.REQUEST_APPROVED, Channel.EMAIL)
        assert "Not sent" not in email and "2 people (1 always, 1 never)" in email
        assert "Not sent" not in cell_of(html, Notice.REQUEST_REJECTED, Channel.IN_APP)

    def test_a_person_who_left_has_no_count(self, people: People) -> None:
        NoticeSetting.objects.create(
            user=people.deactivated,
            notice=Notice.REQUEST_EDITED,
            channel=Channel.EMAIL,
            mode=Mode.NEVER,
            set_by=people.group_admin,
        )
        html = html_of(client_for(people.group_admin), reverse("org:notifications"))
        assert "(0 always, 1 never)" not in html

    def test_the_menu_names_the_page(self, people: People) -> None:
        html = html_of(client_for(people.group_admin), reverse("org:notifications"))
        assert reverse("org:notifications") in html
        assert "Email notifications" not in html and "Notifications" in html
        assert html.count('aria-current="page"') >= 1

    def test_other_roles_do_not_see_the_page_nor_the_menu_entry(self, people: People) -> None:
        for user in (people.mm_mad, people.engineer, people.tech_mad):
            client = client_for(user)
            assert client.get(reverse("org:notifications")).status_code == 403
            assert reverse("org:notifications") not in html_of(client, reverse("core:home"))


def default_data(html: str, notice: Notice, **values: str) -> dict[str, str]:
    """What the dialog of ``notice`` posts: its key, what it showed and what is changed."""
    slug = notice.value.lower()
    shown = {"EMAIL": "on", "IN_APP": "on"}
    data = {"idempotency_key": key_of(html, f"id_notice_{slug}_idempotency_key")}
    data |= shown | {f"seen_{key}": value for key, value in shown.items()}
    return data | values


class TestTheSettingForEveryone:
    def test_turns_a_channel_off_and_on_from_the_dialog(self, people: People) -> None:
        client = client_for(people.group_admin)
        html = html_of(client, reverse("org:notifications"))
        response = client.post(
            default_url(Notice.REQUEST_REJECTED),
            default_data(html, Notice.REQUEST_REJECTED, IN_APP="off"),
            headers=HTMX,
        )
        assert response.status_code == 204
        assert response["HX-Redirect"] == reverse("org:notifications")
        # Only the channel that changed is written: email was left as it showed.
        row = NoticeDefault.objects.get()
        assert (row.notice, row.channel, row.enabled) == (
            Notice.REQUEST_REJECTED,
            Channel.IN_APP,
            False,
        )
        event = AuditEvent.objects.get(action="notification.default_changed")
        assert event.data["channel"] == "IN_APP"
        assert event.data["before"] is True and event.data["after"] is False
        page = html_of(client, reverse("org:notifications"))
        assert "Not sent" in cell_of(page, Notice.REQUEST_REJECTED, Channel.IN_APP)
        assert "Not sent" not in cell_of(page, Notice.REQUEST_REJECTED, Channel.EMAIL)
        # Another operation's key is refused; a fresh key of this one turns it on again.
        other = default_data(page, Notice.REQUEST_REJECTED, IN_APP="on", seen_IN_APP="off")
        other["idempotency_key"] = issue_key(people.group_admin.pk, "x")
        assert client.post(default_url(Notice.REQUEST_REJECTED), other).status_code == 400
        key = issue_key(people.group_admin.pk, NOTICE_CHANNELS_OPERATION)
        data = default_data(page, Notice.REQUEST_REJECTED, IN_APP="on", seen_IN_APP="off")
        client.post(default_url(Notice.REQUEST_REJECTED), data | {"idempotency_key": key})
        assert NoticeDefault.objects.get().enabled

    def test_a_save_changes_only_what_the_dialog_changed(self, people: People) -> None:
        """Another administrator's change made after the dialog was drawn is not undone."""
        client = client_for(people.group_admin)
        html = html_of(client, reverse("org:notifications"))
        services.set_notice_default(
            principal_for(people.group_admin),
            Notice.REQUEST_EDITED,
            enabled=False,
            channel=Channel.EMAIL,
        )
        data = default_data(html, Notice.REQUEST_EDITED, IN_APP="off")
        assert (
            client.post(default_url(Notice.REQUEST_EDITED), data, headers=HTMX).status_code == 204
        )
        rows = {row.channel: row.enabled for row in NoticeDefault.objects.all()}
        assert rows == {Channel.EMAIL: False, Channel.IN_APP: False}

    def test_a_post_without_what_was_shown_names_every_channel(self, people: People) -> None:
        client = client_for(people.group_admin)
        key = issue_key(people.group_admin.pk, NOTICE_CHANNELS_OPERATION)
        data = {"EMAIL": "off", "IN_APP": "off", "idempotency_key": key}
        assert client.post(default_url(Notice.REQUEST_EDITED), data).status_code == 302
        assert {row.channel for row in NoticeDefault.objects.filter(enabled=False)} == set(Channel)

    def test_nothing_changed_writes_and_audits_nothing(self, people: People) -> None:
        client = client_for(people.group_admin)
        html = html_of(client, reverse("org:notifications"))
        response = client.post(
            default_url(Notice.REQUEST_EDITED), default_data(html, Notice.REQUEST_EDITED)
        )
        assert response.status_code == 302
        assert not NoticeDefault.objects.exists()
        assert not AuditEvent.objects.filter(action="notification.default_changed").exists()
        messages = [str(m) for m in get_messages(response.wsgi_request)]
        assert messages == ["Nothing changed."]

    def test_a_double_tap_changes_once(self, people: People) -> None:
        client = client_for(people.group_admin)
        html = html_of(client, reverse("org:notifications"))
        data = default_data(html, Notice.REQUEST_EDITED, EMAIL="off")
        for _ in range(2):
            assert client.post(default_url(Notice.REQUEST_EDITED), data).status_code == 302
        assert AuditEvent.objects.filter(action="notification.default_changed").count() == 1

    def test_an_invalid_value_comes_back_in_the_dialog(self, people: People) -> None:
        client = client_for(people.group_admin)
        data = {
            "EMAIL": "maybe",
            "idempotency_key": issue_key(people.group_admin.pk, NOTICE_CHANNELS_OPERATION),
        }
        response = client.post(default_url(Notice.REQUEST_EDITED), data, headers=HTMX)
        assert response.status_code == 422
        assert b'data-testid="notice-request-edited-form"' in response.content
        assert not NoticeDefault.objects.exists()

    def test_an_unknown_notification_is_a_404_and_a_get_a_405(self, people: People) -> None:
        client = client_for(people.group_admin)
        assert client.post("/org/notifications/NOT_A_NOTICE/", {}).status_code == 404
        assert client.get(default_url(Notice.REQUEST_EDITED)).status_code == 405

    def test_other_roles_are_refused(self, people: People) -> None:
        data = {"EMAIL": "off", "IN_APP": "off"}
        for user in (people.mm_mad, people.engineer, people.tech_mad, people.partner):
            response = client_for(user).post(default_url(Notice.REQUEST_EDITED), data)
            assert response.status_code == 403, user.email
        assert not NoticeDefault.objects.exists()


class TestThePersonsPage:
    def test_the_section_says_what_applies_and_why_on_each_channel(self, people: People) -> None:
        tech, admin = people.tech_mad, people.group_admin
        NoticeDefault.objects.create(
            notice=Notice.REQUEST_REJECTED, channel=Channel.EMAIL, enabled=False, changed_by=admin
        )
        NoticeSetting.objects.create(
            user=tech,
            notice=Notice.REQUEST_APPROVED,
            channel=Channel.IN_APP,
            mode=Mode.NEVER,
            set_by=admin,
        )
        html = html_of(client_for(admin), reverse("org:person", args=[tech.pk]))
        assert 'data-testid="person-notices"' in html
        assert 'data-testid="notices-email-open"' in html
        assert 'data-testid="notices-in-app-open"' in html
        approved = cell_of(html, Notice.REQUEST_APPROVED, Channel.IN_APP)
        assert "Not sent" in approved and "Set for this person: never sent." in approved
        assert "Sent" in cell_of(html, Notice.REQUEST_APPROVED, Channel.EMAIL)
        rejected = cell_of(html, Notice.REQUEST_REJECTED, Channel.EMAIL)
        assert "Not sent" in rejected and "Follows the setting for everyone." in rejected
        edited = state_of(html, Notice.REQUEST_EDITED)
        assert "Sent" in edited and "Not sent" not in edited

    def test_what_their_roles_never_receive_is_marked(self, people: People) -> None:
        html = html_of(
            client_for(people.group_admin), reverse("org:person", args=[people.tech_mad.pk])
        )
        assert "Their current roles do not receive it." in state_of(html, Notice.REQUEST_TO_REVIEW)
        assert "do not receive" not in state_of(html, Notice.REQUEST_APPROVED)
        reviewer = html_of(
            client_for(people.group_admin), reverse("org:person", args=[people.mm_mad.pk])
        )
        assert "do not receive" not in state_of(reviewer, Notice.REQUEST_TO_REVIEW)

    def test_their_own_switch_for_a_channel_is_named_first(self, people: People) -> None:
        NotificationPreference.objects.create(user=people.tech_mad, email_opt_out=True)
        ChannelSwitch.objects.create(user=people.tech_mad, channel=Channel.IN_APP, enabled=False)
        NoticeSetting.objects.create(
            user=people.tech_mad,
            notice=Notice.REQUEST_APPROVED,
            channel=Channel.EMAIL,
            mode=Mode.ALWAYS,
            set_by=people.group_admin,
        )
        html = html_of(
            client_for(people.group_admin), reverse("org:person", args=[people.tech_mad.pk])
        )
        for channel in Channel:
            text = cell_of(html, Notice.REQUEST_APPROVED, channel)
            assert "The person turned this channel off." in text and "Not sent" in text

    def test_their_own_choice_is_shown_with_where_it_overrides(self, people: People) -> None:
        tech, admin = people.tech_mad, people.group_admin
        NoticeSetting.objects.create(
            user=tech,
            notice=Notice.REQUEST_APPROVED,
            channel=Channel.EMAIL,
            mode=Mode.NEVER,
            set_by=admin,
        )
        for notice in (Notice.REQUEST_APPROVED, Notice.REQUEST_REJECTED):
            NoticeChoice.objects.create(
                user=tech, notice=notice, channel=Channel.EMAIL, enabled=True
            )
        html = html_of(client_for(admin), reverse("org:person", args=[tech.pk]))
        approved = cell_of(html, Notice.REQUEST_APPROVED, Channel.EMAIL)
        assert "The person chose this themselves." in approved and "Not sent" not in approved
        assert "It overrides what the administrators set for them." in approved
        # A choice that agrees with the administrators overrides nothing.
        rejected = cell_of(html, Notice.REQUEST_REJECTED, Channel.EMAIL)
        assert "chose this themselves" in rejected and "overrides" not in rejected

    def test_the_dialog_saves_the_choices_of_one_channel(self, people: People) -> None:
        tech, admin = people.tech_mad, people.group_admin
        client = client_for(admin)
        html = html_of(client, reverse("org:person", args=[tech.pk]))
        data = {
            "idempotency_key": key_of(html, "id_notices_email_idempotency_key"),
            "REQUEST_APPROVED": "NEVER",
            "REQUEST_REJECTED": "ALWAYS",
            "REQUEST_EDITED": "",
        }
        response = client.post(person_url(tech), data, headers=HTMX)
        assert response.status_code == 204
        assert response["HX-Redirect"] == reverse("org:person", args=[tech.pk])
        assert {
            (row.notice, row.channel): row.mode for row in NoticeSetting.objects.filter(user=tech)
        } == {
            ("REQUEST_APPROVED", "EMAIL"): "NEVER",
            ("REQUEST_REJECTED", "EMAIL"): "ALWAYS",
        }
        assert resolution.wanted([tech.pk], Notice.REQUEST_APPROVED) == set()
        assert resolution.wanted([tech.pk], Notice.REQUEST_APPROVED, Channel.IN_APP) == {tech.pk}
        # The dialog now starts from what is set.
        again = html_of(client, reverse("org:person", args=[tech.pk]))
        assert re.search(r'<option value="NEVER" selected>', again)
        # Putting it back to follow deletes the row.
        data = {
            "idempotency_key": issue_key(admin.pk, PERSON_NOTICES_OPERATION),
            "REQUEST_APPROVED": "",
            "REQUEST_REJECTED": "",
        }
        client.post(person_url(tech), data)
        assert not NoticeSetting.objects.filter(user=tech).exists()
        assert AuditEvent.objects.filter(action="notification.person_changed").count() == 2

    def test_each_channel_has_its_own_dialog(self, people: People) -> None:
        tech, admin = people.tech_mad, people.group_admin
        client = client_for(admin)
        html = html_of(client, reverse("org:person", args=[tech.pk]))
        key = key_of(html, "id_notices_in_app_idempotency_key")
        data = {"idempotency_key": key, "REQUEST_APPROVED": "NEVER"}
        assert client.post(person_url(tech, Channel.IN_APP), data).status_code == 302
        assert NoticeSetting.objects.get().channel == Channel.IN_APP
        assert resolution.wanted([tech.pk], Notice.REQUEST_APPROVED) == {tech.pk}

    def test_a_save_changes_only_what_the_dialog_changed(self, people: People) -> None:
        """Another administrator's change made after the dialog was drawn is not undone."""
        tech, admin = people.tech_mad, people.group_admin
        client = client_for(admin)
        html = html_of(client, reverse("org:person", args=[tech.pk]))
        assert 'name="seen_REQUEST_APPROVED"' in html
        # Someone else sets one notice while this dialog is open.
        services.set_person_notices(
            principal_for(people.group_admin),
            tech.pk,
            {(Notice.REQUEST_APPROVED, Channel.EMAIL): Mode.NEVER},
        )
        data = {
            "idempotency_key": key_of(html, "id_notices_email_idempotency_key"),
            "REQUEST_EDITED": "ALWAYS",
        }
        for notice in Notice:
            data[f"seen_{notice.value}"] = ""
            data.setdefault(notice.value, "")
        assert client.post(person_url(tech), data, headers=HTMX).status_code == 204
        assert dict(NoticeSetting.objects.filter(user=tech).values_list("notice", "mode")) == {
            "REQUEST_APPROVED": "NEVER",
            "REQUEST_EDITED": "ALWAYS",
        }

    def test_an_unknown_choice_comes_back_in_the_dialog(self, people: People) -> None:
        client = client_for(people.group_admin)
        data = {
            "idempotency_key": issue_key(people.group_admin.pk, PERSON_NOTICES_OPERATION),
            "REQUEST_APPROVED": "SOMETIMES",
        }
        response = client.post(person_url(people.tech_mad), data, headers=HTMX)
        assert response.status_code == 422
        assert b'data-testid="notices-email-form"' in response.content
        assert not NoticeSetting.objects.exists()

    def test_an_administrator_may_set_their_own(self, people: People) -> None:
        admin = people.group_admin
        html = html_of(client_for(admin), reverse("org:person", args=[admin.pk]))
        assert 'data-testid="notices-email-open"' in html
        data = {
            "idempotency_key": issue_key(admin.pk, PERSON_NOTICES_OPERATION),
            "REQUEST_TO_REVIEW": "NEVER",
        }
        client_for(admin).post(person_url(admin), data)
        assert NoticeSetting.objects.get(user=admin).mode == Mode.NEVER

    def test_nothing_for_a_staff_account_nor_one_that_left(self, people: People) -> None:
        client = client_for(people.group_admin)
        for user in (people.operator, people.deactivated):
            html = html_of(client, reverse("org:person", args=[user.pk]))
            assert 'data-testid="person-notices"' not in html, user.email
        data = {
            "idempotency_key": issue_key(people.group_admin.pk, PERSON_NOTICES_OPERATION),
            "REQUEST_APPROVED": "NEVER",
        }
        assert client.post(person_url(people.operator), data).status_code == 403
        assert not NoticeSetting.objects.exists()

    def test_a_person_out_of_reach_or_a_channel_unknown_is_a_404_and_other_roles_a_403(
        self, people: People
    ) -> None:
        data = {"REQUEST_APPROVED": "NEVER"}
        missing = reverse(
            "org:person_notifications", args=["00000000-0000-7000-8000-000000000000", "email"]
        )
        assert client_for(people.group_admin).post(missing, data).status_code == 404
        unknown = reverse("org:person_notifications", args=[people.tech_mad.pk, "telegram"])
        assert client_for(people.group_admin).post(unknown, data).status_code == 404
        for user in (people.mm_mad, people.engineer, people.tech_mad):
            assert client_for(user).post(person_url(people.partner), data).status_code == 403

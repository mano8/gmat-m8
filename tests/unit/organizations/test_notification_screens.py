"""Which business emails are sent: the administration page, the person's page and one's own page
(M15.3, Q87-Q89).

A group administrator sets the emails for everyone and for a person from dialogs; the person
sees, read-only, what applies to them beside their own switch. Nobody else reads another
person's settings.
"""

import re

import pytest
from django.test import Client
from django.urls import reverse
from django.utils import translation
from django.utils.html import escape

from apps.accounts.models import User
from apps.audit.models import AuditEvent
from apps.core.idempotency import issue_key
from apps.notifications import resolution, services
from apps.notifications.kinds import NOTICES, Channel, Mode, Notice
from apps.notifications.models import NoticeDefault, NoticeSetting, NotificationPreference
from apps.notifications.services import NOTICE_DEFAULT_OPERATION, PERSON_NOTICES_OPERATION
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


def person_url(user: User) -> str:
    return reverse("org:person_notifications", args=[user.pk])


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
    row = re.search(rf'data-notice="{notice.value}".*?</(?:tr|li)>', html, re.S)
    assert row is not None, notice
    return " ".join(re.sub(r"<[^>]+>", " ", row.group(0)).split())


class TestAdministrationPage:
    def test_every_email_in_the_registry_order_with_who_and_when(self, people: People) -> None:
        html = html_of(client_for(people.group_admin), reverse("org:notifications"))
        positions = [html.index(f'data-notice="{notice.value}"') for notice in NOTICES]
        assert positions == sorted(positions)
        with translation.override("en"):
            for spec in NOTICES.values():
                assert escape(str(spec.recipients)) in html and escape(str(spec.when)) in html

    def test_all_on_at_first_and_no_own_settings(self, people: People) -> None:
        html = html_of(client_for(people.group_admin), reverse("org:notifications"))
        for notice in NOTICES:
            text = state_of(html, notice)
            assert "Not sent" not in text and "Sent" in text
        assert "Turn off for everyone" in html and "Turn on for everyone" not in html

    def test_an_email_turned_off_offers_to_turn_it_on_and_counts_own_settings(
        self, people: People
    ) -> None:
        admin = people.group_admin
        NoticeDefault.objects.create(
            notice=Notice.REQUEST_APPROVED, channel=Channel.EMAIL, enabled=False, changed_by=admin
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
        text = state_of(html, Notice.REQUEST_APPROVED)
        assert "Not sent" in text and "Turn on for everyone" in text
        assert "2 people (1 always, 1 never)" in text
        assert "Turn off for everyone" in state_of(html, Notice.REQUEST_REJECTED) + html

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
        assert reverse("org:notifications") in html and "Email notifications" in html
        assert html.count('aria-current="page"') >= 1

    def test_other_roles_do_not_see_the_page_nor_the_menu_entry(self, people: People) -> None:
        for user in (people.mm_mad, people.engineer, people.tech_mad):
            client = client_for(user)
            assert client.get(reverse("org:notifications")).status_code == 403
            assert reverse("org:notifications") not in html_of(client, reverse("core:home"))


class TestTheSettingForEveryone:
    def test_turns_an_email_off_and_on_from_its_dialog(self, people: People) -> None:
        client = client_for(people.group_admin)
        html = html_of(client, reverse("org:notifications"))
        key = key_of(html, "id_notice_request_rejected_idempotency_key")
        response = client.post(
            default_url(Notice.REQUEST_REJECTED),
            {"enabled": "off", "idempotency_key": key},
            headers=HTMX,
        )
        assert response.status_code == 204
        assert response["HX-Redirect"] == reverse("org:notifications")
        assert not NoticeDefault.objects.get(notice=Notice.REQUEST_REJECTED).enabled
        event = AuditEvent.objects.get(action="notification.default_changed")
        assert event.data["before"] is True and event.data["after"] is False
        assert "Not sent" in state_of(
            html_of(client, reverse("org:notifications")), Notice.REQUEST_REJECTED
        )
        again = client.post(
            default_url(Notice.REQUEST_REJECTED),
            {"enabled": "on", "idempotency_key": issue_key(people.group_admin.pk, "x")},
        )
        assert again.status_code == 400  # a key of another operation is refused
        key = issue_key(people.group_admin.pk, NOTICE_DEFAULT_OPERATION)
        client.post(default_url(Notice.REQUEST_REJECTED), {"enabled": "on", "idempotency_key": key})
        assert NoticeDefault.objects.get(notice=Notice.REQUEST_REJECTED).enabled

    def test_a_double_tap_changes_once(self, people: People) -> None:
        client = client_for(people.group_admin)
        data = {
            "enabled": "off",
            "idempotency_key": issue_key(people.group_admin.pk, NOTICE_DEFAULT_OPERATION),
        }
        for _ in range(2):
            assert client.post(default_url(Notice.REQUEST_EDITED), data).status_code == 302
        assert AuditEvent.objects.filter(action="notification.default_changed").count() == 1

    def test_an_invalid_value_comes_back_in_the_dialog(self, people: People) -> None:
        client = client_for(people.group_admin)
        data = {
            "enabled": "maybe",
            "idempotency_key": issue_key(people.group_admin.pk, NOTICE_DEFAULT_OPERATION),
        }
        response = client.post(default_url(Notice.REQUEST_EDITED), data, headers=HTMX)
        assert response.status_code == 422
        assert b'data-testid="notice-request-edited-form"' in response.content
        assert not NoticeDefault.objects.exists()
        # The notice is on, so the dialog that comes back still offers to turn it off.
        assert "Turn off</button>" in response.content.decode()
        assert "Turn on</button>" not in response.content.decode()

    def test_an_unknown_email_is_a_404_and_a_get_a_405(self, people: People) -> None:
        client = client_for(people.group_admin)
        assert client.post("/org/notifications/NOT_AN_EMAIL/", {}).status_code == 404
        assert client.get(default_url(Notice.REQUEST_EDITED)).status_code == 405

    def test_other_roles_are_refused(self, people: People) -> None:
        data = {"enabled": "off"}
        for user in (people.mm_mad, people.engineer, people.tech_mad, people.partner):
            response = client_for(user).post(default_url(Notice.REQUEST_EDITED), data)
            assert response.status_code == 403, user.email
        assert not NoticeDefault.objects.exists()


class TestThePersonsPage:
    def test_the_section_says_what_applies_and_why(self, people: People) -> None:
        tech, admin = people.tech_mad, people.group_admin
        NoticeDefault.objects.create(
            notice=Notice.REQUEST_REJECTED, channel=Channel.EMAIL, enabled=False, changed_by=admin
        )
        NoticeSetting.objects.create(
            user=tech,
            notice=Notice.REQUEST_APPROVED,
            channel=Channel.EMAIL,
            mode=Mode.NEVER,
            set_by=admin,
        )
        html = html_of(client_for(admin), reverse("org:person", args=[tech.pk]))
        assert 'data-testid="person-notices"' in html and 'data-testid="notices-open"' in html
        approved = state_of(html, Notice.REQUEST_APPROVED)
        assert "Not sent" in approved and "Set for this person: never sent." in approved
        rejected = state_of(html, Notice.REQUEST_REJECTED)
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

    def test_their_own_refusal_is_named_first(self, people: People) -> None:
        NotificationPreference.objects.create(user=people.tech_mad, email_opt_out=True)
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
        text = state_of(html, Notice.REQUEST_APPROVED)
        assert "turned business email off themselves" in text and "Not sent" in text

    def test_the_dialog_saves_the_choices(self, people: People) -> None:
        tech, admin = people.tech_mad, people.group_admin
        client = client_for(admin)
        html = html_of(client, reverse("org:person", args=[tech.pk]))
        data = {
            "idempotency_key": key_of(html, "id_notices_idempotency_key"),
            "REQUEST_APPROVED": "NEVER",
            "REQUEST_REJECTED": "ALWAYS",
            "REQUEST_EDITED": "",
        }
        response = client.post(person_url(tech), data, headers=HTMX)
        assert response.status_code == 204
        assert response["HX-Redirect"] == reverse("org:person", args=[tech.pk])
        assert dict(NoticeSetting.objects.filter(user=tech).values_list("notice", "mode")) == {
            "REQUEST_APPROVED": "NEVER",
            "REQUEST_REJECTED": "ALWAYS",
        }
        assert resolution.wanted([tech.pk], Notice.REQUEST_APPROVED) == set()
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
            "idempotency_key": key_of(html, "id_notices_idempotency_key"),
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
        assert b'data-testid="notices-form"' in response.content
        assert not NoticeSetting.objects.exists()

    def test_an_administrator_may_set_their_own(self, people: People) -> None:
        admin = people.group_admin
        html = html_of(client_for(admin), reverse("org:person", args=[admin.pk]))
        assert 'data-testid="notices-open"' in html
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

    def test_a_person_out_of_reach_is_a_404_and_other_roles_a_403(self, people: People) -> None:
        data = {"REQUEST_APPROVED": "NEVER"}
        missing = reverse("org:person_notifications", args=["00000000-0000-7000-8000-000000000000"])
        assert client_for(people.group_admin).post(missing, data).status_code == 404
        for user in (people.mm_mad, people.engineer, people.tech_mad):
            assert client_for(user).post(person_url(people.partner), data).status_code == 403


class TestOwnPage:
    def test_what_an_administrator_set_is_shown_beside_the_switch(self, people: People) -> None:
        tech = people.tech_mad
        NoticeDefault.objects.create(
            notice=Notice.REQUEST_REJECTED,
            channel=Channel.EMAIL,
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
        html = html_of(client_for(tech), reverse("notifications:settings"))
        assert 'data-testid="notification-settings"' in html
        assert 'data-testid="own-notices"' in html
        approved = state_of(html, Notice.REQUEST_APPROVED)
        assert "An administrator set this email never to reach you." in approved
        assert "Not sent" in approved
        assert "Follows the setting for everyone." in state_of(html, Notice.REQUEST_REJECTED)
        assert "Not sent" in state_of(html, Notice.REQUEST_REJECTED)
        assert "Your current roles do not receive it." in state_of(html, Notice.REQUEST_TO_REVIEW)

    def test_your_own_refusal_wins_and_says_so(self, people: People) -> None:
        NotificationPreference.objects.create(user=people.mm_mad, email_opt_out=True)
        html = html_of(client_for(people.mm_mad), reverse("notifications:settings"))
        assert "You turned business email off." in state_of(html, Notice.REQUEST_TO_REVIEW)

    def test_it_is_read_only_and_the_page_offers_no_way_to_change_it(self, people: People) -> None:
        html = html_of(client_for(people.tech_mad), reverse("notifications:settings"))
        assert reverse("org:person_notifications", args=[people.tech_mad.pk]) not in html
        assert "notices-open" not in html

    def test_nothing_of_another_person_reaches_a_page(self, people: People) -> None:
        NoticeSetting.objects.create(
            user=people.mm_mad,
            notice=Notice.REQUEST_TO_REVIEW,
            channel=Channel.EMAIL,
            mode=Mode.NEVER,
            set_by=people.group_admin,
        )
        html = html_of(client_for(people.tech_mad), reverse("notifications:settings"))
        assert people.mm_mad.email not in html
        assert "never to reach you" not in state_of(html, Notice.REQUEST_TO_REVIEW)

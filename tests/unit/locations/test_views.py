"""Warehouse and worksite screens (M10.3, inventory §3.3).

Every action goes through the screen as a group administrator would send it (a plain POST and
the dialog's htmx POST); the service decides, and its refusals reach the dialog with their
reason. Other actors get 403, unknown or out-of-kind places 404, other methods 405.
"""

import re
from collections.abc import Callable
from typing import Any

import pytest
from django.conf import settings
from django.test import Client
from django.urls import reverse
from django.utils import timezone, translation
from django.utils.translation import gettext

from apps.access.context import AccessContext
from apps.audit.models import AuditEvent
from apps.core.idempotency import issue_key
from apps.locations.models import Location
from apps.organizations.models import Branch, Membership
from apps.organizations.services import update_branch
from tests.org import Org, People, client_for

pytestmark = pytest.mark.django_db

HTMX = {"HX-Request": "true"}
UNKNOWN = "00000000-0000-7000-8000-000000000000"


def in_dialog(dialog: str) -> dict[str, str]:
    return {**HTMX, "HX-Target": f"{dialog}-content"}


def codes(response: Any) -> set[str]:
    form = response.context["d"].form
    return {error.code for errors in form.errors.as_data().values() for error in errors}


def names(response: Any) -> list[str]:
    return re.findall(r'<span class="font-medium">([^<]+)</span>', response.content.decode())


@pytest.fixture
def admin(people: People) -> Client:
    return client_for(people.group_admin)


@pytest.fixture
def mad(org: Org) -> Location:
    return org.warehouses["A Madrid"]


def key(people: People) -> str:
    return issue_key(people.group_admin.pk, "location.create_warehouse")


def last_event(action: str) -> AuditEvent:
    return AuditEvent.objects.filter(action=action).order_by("-occurred_at").first()  # type: ignore[return-value]


# --- who may ---------------------------------------------------------------------------------

GETS: list[tuple[str, Callable[[Org], list[object]]]] = [
    ("locations:warehouses", lambda org: []),
    ("locations:new_warehouse", lambda org: []),
    ("locations:edit_warehouse", lambda org: [org.warehouses["A Madrid"].pk]),
    ("locations:confirm_deactivate_warehouse", lambda org: [org.warehouses["A Madrid"].pk]),
]
POSTS: list[tuple[str, Callable[[Org], list[object]]]] = [
    ("locations:create_warehouse", lambda org: []),
    ("locations:update_warehouse", lambda org: [org.warehouses["A Madrid"].pk]),
    ("locations:deactivate_warehouse", lambda org: [org.warehouses["A Madrid"].pk]),
    ("locations:reactivate_warehouse", lambda org: [org.warehouses["A Madrid"].pk]),
    ("locations:update_worksite", lambda org: [org.worksite(org.hospital).pk]),
    ("locations:deactivate_worksite", lambda org: [org.worksite(org.hospital).pk]),
    ("locations:reactivate_worksite", lambda org: [org.worksite(org.hospital).pk]),
]


class TestWhoMay:
    @pytest.mark.parametrize(
        "who", ["mm_mad", "engineer", "operator", "wo_mad", "tech_mad", "site_manager", "partner"]
    )
    def test_everyone_else_is_refused(self, org: Org, people: People, who: str) -> None:
        client = client_for(getattr(people, who))
        for name, args in GETS:
            assert client.get(reverse(name, args=args(org))).status_code == 403, name
        for name, args in POSTS:
            assert client.post(reverse(name, args=args(org))).status_code == 403, name

    def test_other_methods_are_405(self, org: Org, admin: Client) -> None:
        for name, args in POSTS:
            assert admin.get(reverse(name, args=args(org))).status_code == 405, name
        for name, args in GETS:
            assert admin.post(reverse(name, args=args(org))).status_code == 405, name

    def test_an_unknown_place_is_404(self, admin: Client) -> None:
        for name in ("edit_warehouse", "confirm_deactivate_warehouse"):
            assert admin.get(reverse(f"locations:{name}", args=[UNKNOWN])).status_code == 404
        for name in ("update_warehouse", "deactivate_worksite", "reactivate_worksite"):
            assert admin.post(reverse(f"locations:{name}", args=[UNKNOWN])).status_code == 404

    def test_a_worksite_is_not_a_warehouse_and_the_other_way_round(
        self, org: Org, admin: Client, mad: Location
    ) -> None:
        site = org.worksite(org.hospital)
        assert admin.get(reverse("locations:edit_warehouse", args=[site.pk])).status_code == 404
        assert admin.post(reverse("locations:update_warehouse", args=[site.pk])).status_code == 404
        assert admin.post(reverse("locations:update_worksite", args=[mad.pk])).status_code == 404
        assert (
            admin.post(reverse("locations:deactivate_worksite", args=[mad.pk])).status_code == 404
        )

    def test_a_revoked_administrator_is_refused(
        self, people: People, admin: Client, mad: Location
    ) -> None:
        assert admin.get(reverse("locations:warehouses")).status_code == 200
        Membership.objects.filter(user=people.group_admin).update(revoked_at=timezone.now())
        response = admin.post(reverse("locations:deactivate_warehouse", args=[mad.pk]))
        assert response.status_code == 403
        assert Location.objects.get(pk=mad.pk).is_active

    def test_without_session_mfa_the_administrator_steps_up(self, people: People) -> None:
        client = client_for(people.group_admin, mfa=False)
        assert client.get(reverse("locations:warehouses")).status_code == 302


# --- the list --------------------------------------------------------------------------------


class TestList:
    def test_lists_the_warehouses_never_the_worksites(self, org: Org, admin: Client) -> None:
        response = admin.get(reverse("locations:warehouses"))
        assert sorted(names(response)) == ["wh A Madrid", "wh A Sevilla", "wh B Madrid"]
        assert "site HOSP-X" not in response.content.decode()

    def test_filters_and_partial(self, org: Org, admin: Client, mad: Location) -> None:
        Location.objects.filter(pk=org.warehouses["A Sevilla"].pk).update(is_active=False)
        url = reverse("locations:warehouses")
        assert names(admin.get(url, {"state": "inactive"})) == ["wh A Sevilla"]
        assert "wh A Sevilla" not in names(admin.get(url, {"state": "active"}))
        assert names(admin.get(url, {"branch": str(org.b_mad.pk)})) == ["wh B Madrid"]
        assert names(admin.get(url, {"q": "sevilla"})) == ["wh A Sevilla"]
        assert sorted(names(admin.get(url, {"q": "MAD"}))) == ["wh A Madrid", "wh B Madrid"]
        assert names(admin.get(url, {"q": "B Mad"})) == ["wh B Madrid"]
        partial = admin.get(url, {"q": "madrid"}, headers=HTMX)
        assert partial.content.lstrip().startswith(b'<div id="warehouse-results">')

    def test_the_default_is_marked_and_a_row_offers_what_it_may_do(
        self, org: Org, admin: Client, mad: Location, admin_principal: Any
    ) -> None:
        update_branch(admin_principal, org.a_mad.pk, default_warehouse_id=mad.pk)
        response = admin.get(reverse("locations:warehouses"), {"q": "wh A Madrid"})
        (row,) = response.context["listing"].rows
        assert row.is_default
        page = response.content.decode()
        assert reverse("locations:confirm_deactivate_warehouse", args=[mad.pk]) in page
        assert reverse("locations:reactivate_warehouse", args=[mad.pk]) not in page
        Location.objects.filter(pk=mad.pk).update(is_active=False)
        again = admin.get(reverse("locations:warehouses"), {"q": "wh A Madrid"}).content.decode()
        assert reverse("locations:reactivate_warehouse", args=[mad.pk]) in again
        assert reverse("locations:confirm_deactivate_warehouse", args=[mad.pk]) not in again

    def test_a_warehouse_of_an_inactive_branch_is_listed_to_be_reactivated(
        self, org: Org, admin: Client, admin_principal: Any
    ) -> None:
        old = Branch.objects.get(code="OLD")
        place = Location.objects.create(kind=Location.Kind.WAREHOUSE, branch=old, name="Old wh")
        assert "Old wh" in names(admin.get(reverse("locations:warehouses")))
        assert place.is_active


# --- create ----------------------------------------------------------------------------------


class TestCreate:
    def test_the_lazy_dialog_offers_active_branches_only(self, org: Org, admin: Client) -> None:
        url = reverse("locations:new_warehouse")
        fragment = admin.get(url, headers=in_dialog("new-dialog"))
        assert fragment.status_code == 200 and b"<html" not in fragment.content
        assert b'data-testid="warehouse-new-form"' in fragment.content
        offered = {b.pk for b in fragment.context["d"].form.fields["branch"].queryset}
        assert org.a_mad.pk in offered and Branch.objects.get(code="OLD").pk not in offered
        assert b"<html" in admin.get(url).content

    def test_from_a_branch_page_the_branch_is_chosen_and_cancel_returns_there(
        self, org: Org, admin: Client
    ) -> None:
        response = admin.get(f"{reverse('locations:new_warehouse')}?branch={org.a_sev.pk}")
        dialog = response.context["d"]
        assert dialog.form.initial["branch"] == org.a_sev.pk
        assert dialog.back == reverse("org:branch", args=[org.a_sev.pk])
        for wanted in ("junk", str(Branch.objects.get(code="OLD").pk)):  # not offered: ignored
            other = admin.get(reverse("locations:new_warehouse"), {"branch": wanted})
            assert "branch" not in other.context["d"].form.initial

    def test_creates_it(self, org: Org, people: People, admin: Client) -> None:
        response = admin.post(
            reverse("locations:create_warehouse"),
            {
                "branch": str(org.c_hq.pk),
                "name": "Almacén C",
                "address": "Calle 1",
                "idempotency_key": key(people),
            },
            headers=HTMX,
        )
        made = Location.objects.get(name="Almacén C")
        assert response.status_code == 204
        assert response["HX-Redirect"] == reverse("locations:warehouses")
        assert (made.kind, made.branch_id, made.address) == (
            Location.Kind.WAREHOUSE,
            org.c_hq.pk,
            "Calle 1",
        )
        assert last_event("location.created").actor_id == people.group_admin.pk

    def test_a_second_tap_creates_once(self, org: Org, people: People, admin: Client) -> None:
        data = {"branch": str(org.c_hq.pk), "name": "Almacén C", "idempotency_key": key(people)}
        first = admin.post(reverse("locations:create_warehouse"), data)
        second = admin.post(reverse("locations:create_warehouse"), data)
        assert first.status_code == second.status_code == 302
        assert Location.objects.filter(name="Almacén C").count() == 1

    def test_an_inactive_branch_is_not_a_choice(self, people: People, admin: Client) -> None:
        old = Branch.objects.get(code="OLD")
        response = admin.post(
            reverse("locations:create_warehouse"),
            {"branch": str(old.pk), "name": "X", "idempotency_key": key(people)},
            headers=HTMX,
        )
        assert response.status_code == 422
        assert response.context["d"].form.has_error("branch")
        assert not Location.objects.filter(name="X").exists()

    def test_the_service_refuses_a_branch_that_went_inactive_after_the_form(
        self, people: People, admin: Client, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The form offered the branch; it is deactivated before the submit reaches the
        service, which says so on the field (a refusal, never a 500)."""
        monkeypatch.setattr(
            "apps.locations.forms.selectors.new_warehouse_branches",
            lambda ctx: Branch.objects.select_related("company"),
        )
        old = Branch.objects.get(code="OLD")
        response = admin.post(
            reverse("locations:create_warehouse"),
            {"branch": str(old.pk), "name": "X", "idempotency_key": key(people)},
            headers=HTMX,
        )
        assert response.status_code == 422 and codes(response) == {"inactive"}

    def test_an_empty_form_is_shown_again(self, admin: Client) -> None:
        assert admin.post(reverse("locations:create_warehouse"), headers=HTMX).status_code == 422
        plain = admin.post(reverse("locations:create_warehouse"))
        assert plain.status_code == 302 and plain["Location"] == reverse("locations:warehouses")


# --- edit ------------------------------------------------------------------------------------


class TestEdit:
    def test_the_dialog_holds_the_data(self, admin: Client, mad: Location) -> None:
        response = admin.get(
            reverse("locations:edit_warehouse", args=[mad.pk]), headers=in_dialog("edit-dialog")
        )
        assert response.status_code == 200 and b"<html" not in response.content
        assert response.context["d"].form.initial == {"name": "wh A Madrid", "address": ""}

    def test_saves_name_and_address(self, people: People, admin: Client, mad: Location) -> None:
        response = admin.post(
            reverse("locations:update_warehouse", args=[mad.pk]),
            {"name": "Central", "address": "Polígono 3"},
            headers=HTMX,
        )
        assert response.status_code == 204
        mad.refresh_from_db()
        assert (mad.name, mad.address, mad.is_active) == ("Central", "Polígono 3", True)
        event = last_event("location.updated")
        assert event.actor_id == people.group_admin.pk
        assert event.data["name"] == {"from": "wh A Madrid", "to": "Central"}

    def test_an_invalid_form_is_shown_again(self, admin: Client, mad: Location) -> None:
        url = reverse("locations:update_warehouse", args=[mad.pk])
        assert admin.post(url, {"name": ""}, headers=HTMX).status_code == 422
        assert admin.post(url, {"name": ""})["Location"] == reverse("locations:warehouses")


# --- activity --------------------------------------------------------------------------------


class TestActivity:
    def test_the_dialog_warns_about_the_default(
        self, org: Org, admin: Client, mad: Location, admin_principal: Any
    ) -> None:
        url = reverse("locations:confirm_deactivate_warehouse", args=[mad.pk])
        plain = admin.get(url, headers=in_dialog("activity-dialog"))
        assert b'data-testid="default-warning"' not in plain.content
        update_branch(admin_principal, org.a_mad.pk, default_warehouse_id=mad.pk)
        warned = admin.get(url, headers=in_dialog("activity-dialog"))
        assert b'data-testid="default-warning"' in warned.content

    def test_deactivates_and_reactivates(
        self, people: People, admin: Client, mad: Location
    ) -> None:
        response = admin.post(
            reverse("locations:deactivate_warehouse", args=[mad.pk]), headers=HTMX
        )
        assert response.status_code == 204
        assert not Location.objects.get(pk=mad.pk).is_active
        again = admin.post(reverse("locations:reactivate_warehouse", args=[mad.pk]))
        assert again.status_code == 302 and Location.objects.get(pk=mad.pk).is_active
        assert {e.actor_id for e in AuditEvent.objects.filter(action="location.updated")} == {
            people.group_admin.pk
        }

    def test_the_default_warehouse_refusal_reaches_the_dialog(
        self, org: Org, admin: Client, mad: Location, admin_principal: Any
    ) -> None:
        update_branch(admin_principal, org.a_mad.pk, default_warehouse_id=mad.pk)
        response = admin.post(
            reverse("locations:deactivate_warehouse", args=[mad.pk]), headers=HTMX
        )
        assert response.status_code == 422 and codes(response) == {"default_warehouse"}
        assert Location.objects.get(pk=mad.pk).is_active
        plain = admin.post(reverse("locations:deactivate_warehouse", args=[mad.pk]))
        assert plain.status_code == 302 and plain["Location"] == reverse("locations:warehouses")
        messages = [str(m) for m in admin.get(reverse("locations:warehouses")).context["messages"]]
        with translation.override(settings.LANGUAGE_CODE):
            refusal = gettext("This is the branch's default warehouse; choose another first.")
        assert refusal in messages

    def test_after_another_default_it_may_be_deactivated(
        self, org: Org, admin: Client, mad: Location, admin_principal: Any
    ) -> None:
        other = Location.objects.create(
            kind=Location.Kind.WAREHOUSE, branch=org.a_mad, name="Second"
        )
        update_branch(admin_principal, org.a_mad.pk, default_warehouse_id=mad.pk)
        update_branch(admin_principal, org.a_mad.pk, default_warehouse_id=other.pk)
        response = admin.post(
            reverse("locations:deactivate_warehouse", args=[mad.pk]), headers=HTMX
        )
        assert response.status_code == 204


# --- the branch page -------------------------------------------------------------------------


class TestBranchPage:
    def test_offers_a_new_warehouse_for_that_branch_and_the_list(
        self, org: Org, admin: Client
    ) -> None:
        page = admin.get(reverse("org:branch", args=[org.a_sev.pk])).content.decode()
        assert f"{reverse('locations:new_warehouse')}?branch={org.a_sev.pk}" in page
        assert f"{reverse('locations:warehouses')}?branch={org.a_sev.pk}" in page
        assert 'data-testid="warehouse-new-open"' in page

    def test_an_inactive_branch_offers_none(self, admin: Client) -> None:
        old = Branch.objects.get(code="OLD")
        page = admin.get(reverse("org:branch", args=[old.pk])).content.decode()
        assert 'data-testid="warehouse-new-open"' not in page


# --- worksites, from the project's page ------------------------------------------------------


class TestWorksite:
    def test_the_project_page_offers_the_dialogs_to_group_administrators_only(
        self, org: Org, people: People, admin: Client
    ) -> None:
        url = reverse("projects:detail", args=[org.hospital.pk])
        page = admin.get(url).content.decode()
        assert 'data-testid="worksite"' in page and "site HOSP-X" in page
        assert reverse("locations:update_worksite", args=[org.worksite(org.hospital).pk]) in page
        assert 'data-testid="worksite-deactivate-open"' in page
        for who in ("mm_mad", "engineer", "site_manager", "partner", "tech_mad"):
            seen = client_for(getattr(people, who)).get(url)
            assert seen.status_code == 200, who
            assert 'data-testid="worksite"' not in seen.content.decode(), who
            assert "locations/worksites" not in seen.content.decode(), who

    def test_renames_and_readdresses_it(self, org: Org, people: People, admin: Client) -> None:
        site = org.worksite(org.hospital)
        response = admin.post(
            reverse("locations:update_worksite", args=[site.pk]),
            {"name": "Hospital gate", "address": "Calle Mayor 1"},
            headers=HTMX,
        )
        assert response.status_code == 204
        assert response["HX-Redirect"] == reverse("projects:detail", args=[org.hospital.pk])
        site.refresh_from_db()
        assert (site.name, site.address, site.project_id) == (
            "Hospital gate",
            "Calle Mayor 1",
            org.hospital.pk,
        )
        assert last_event("location.updated").actor_id == people.group_admin.pk
        page = admin.get(reverse("projects:detail", args=[org.hospital.pk])).content.decode()
        assert "Hospital gate" in page

    def test_deactivating_and_reactivating_it(self, org: Org, admin: Client) -> None:
        site = org.worksite(org.hospital)
        done = admin.post(reverse("locations:deactivate_worksite", args=[site.pk]), headers=HTMX)
        assert done.status_code == 204 and not Location.objects.get(pk=site.pk).is_active
        page = admin.get(reverse("projects:detail", args=[org.hospital.pk])).content.decode()
        assert 'data-testid="worksite-reactivate-open"' in page
        assert 'data-testid="worksite-deactivate-open"' not in page
        back = admin.post(reverse("locations:reactivate_worksite", args=[site.pk]))
        assert back.status_code == 302
        assert back["Location"] == reverse("projects:detail", args=[org.hospital.pk])
        assert Location.objects.get(pk=site.pk).is_active

    def test_an_inactive_worksite_is_no_place_to_receive(
        self, org: Org, people: People, admin: Client
    ) -> None:
        from apps.receiving import selectors as receiving_selectors

        site = org.worksite(org.hospital)
        admin.post(reverse("locations:deactivate_worksite", args=[site.pk]))
        ctx = AccessContext.for_user(people.group_admin)
        assert receiving_selectors.worksite(ctx, org.hospital.pk, "receipt.view") is None

    def test_an_invalid_form_is_shown_again(self, org: Org, admin: Client) -> None:
        site = org.worksite(org.hospital)
        url = reverse("locations:update_worksite", args=[site.pk])
        assert admin.post(url, {"name": ""}, headers=HTMX).status_code == 422
        flashed = admin.post(url, {"name": ""})
        assert flashed["Location"] == reverse("projects:detail", args=[org.hospital.pk])
        assert Location.objects.get(pk=site.pk).name == "site HOSP-X"

    def test_a_project_without_a_worksite_row_shows_no_section(
        self, org: Org, admin: Client
    ) -> None:
        Location.objects.filter(project=org.mad_hold).delete()
        page = admin.get(reverse("projects:detail", args=[org.mad_hold.pk])).content.decode()
        assert 'data-testid="worksite"' not in page

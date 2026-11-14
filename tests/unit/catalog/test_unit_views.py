"""Units of measure screens (M10.3, inventory §3.5, Q72).

Every action goes through the screen as a group administrator would send it (a plain POST and
the dialog's htmx POST); the services decide, and their refusals reach the dialog with their
reason. Other actors get 403, unknown objects 404, other methods 405.
"""

import re
from collections.abc import Callable
from typing import Any

import pytest
from django.test import Client
from django.urls import reverse

from apps.audit.models import AuditEvent
from apps.catalog import services
from apps.catalog.models import Material, UnitOfMeasure
from apps.core.idempotency import issue_key
from tests.org import Org, People, client_for

pytestmark = pytest.mark.django_db

HTMX = {"HX-Request": "true"}
UNKNOWN = "00000000-0000-7000-8000-000000000000"


def in_dialog(dialog: str) -> dict[str, str]:
    return {**HTMX, "HX-Target": f"{dialog}-content"}


def codes(response: Any) -> set[str]:
    form = response.context["d"].form
    return {error.code for errors in form.errors.as_data().values() for error in errors}


def listed(response: Any) -> list[str]:
    return re.findall(r'<span class="doc-no">([^<]+)</span>', response.content.decode())


@pytest.fixture
def admin(people: People) -> Client:
    return client_for(people.group_admin)


@pytest.fixture
def pallet(admin_principal: Any) -> UnitOfMeasure:
    return services.create_unit(admin_principal, code="PAL", name="Palet", decimals=1)


@pytest.fixture
def used(admin_principal: Any, pallet: UnitOfMeasure) -> UnitOfMeasure:
    services.create_material(admin_principal, reference="M-1", name="Thing", unit_id=pallet.pk)
    return pallet


def key(people: People) -> str:
    return issue_key(people.group_admin.pk, "unit.create")


# --- who may ---------------------------------------------------------------------------------

GETS: list[tuple[str, Callable[[UnitOfMeasure], list[object]]]] = [
    ("catalog:units", lambda unit: []),
    ("catalog:new_unit", lambda unit: []),
    ("catalog:edit_unit", lambda unit: [unit.pk]),
]
POSTS: list[tuple[str, Callable[[UnitOfMeasure], list[object]]]] = [
    ("catalog:create_unit", lambda unit: []),
    ("catalog:update_unit", lambda unit: [unit.pk]),
    ("catalog:deactivate_unit", lambda unit: [unit.pk]),
    ("catalog:reactivate_unit", lambda unit: [unit.pk]),
]


class TestWhoMay:
    @pytest.mark.parametrize(
        "who", ["mm_mad", "engineer", "operator", "wo_mad", "tech_mad", "site_manager", "partner"]
    )
    def test_everyone_else_is_refused(
        self, people: People, pallet: UnitOfMeasure, who: str
    ) -> None:
        client = client_for(getattr(people, who))
        for name, args in GETS:
            assert client.get(reverse(name, args=args(pallet))).status_code == 403, name
        for name, args in POSTS:
            assert client.post(reverse(name, args=args(pallet))).status_code == 403, name
        assert UnitOfMeasure.objects.get(pk=pallet.pk).is_active

    def test_other_methods_are_405(self, admin: Client, pallet: UnitOfMeasure) -> None:
        for name, args in POSTS:
            assert admin.get(reverse(name, args=args(pallet))).status_code == 405, name
        for name, args in GETS:
            assert admin.post(reverse(name, args=args(pallet))).status_code == 405, name

    @pytest.mark.parametrize(
        "name", ["catalog:edit_unit", "catalog:update_unit", "catalog:deactivate_unit"]
    )
    def test_an_unknown_unit_is_404(self, admin: Client, name: str) -> None:
        method = admin.get if name == "catalog:edit_unit" else admin.post
        assert method(reverse(name, args=[UNKNOWN])).status_code == 404

    def test_a_revoked_administrator_is_refused(
        self, people: People, pallet: UnitOfMeasure, admin: Client
    ) -> None:
        from django.utils import timezone

        from apps.organizations.models import Membership

        assert admin.get(reverse("catalog:units")).status_code == 200
        Membership.objects.filter(user=people.group_admin).update(revoked_at=timezone.now())
        assert admin.post(reverse("catalog:deactivate_unit", args=[pallet.pk])).status_code == 403
        assert UnitOfMeasure.objects.get(pk=pallet.pk).is_active

    def test_without_session_mfa_the_administrator_steps_up(self, people: People) -> None:
        client = client_for(people.group_admin, mfa=False)
        assert client.get(reverse("catalog:units")).status_code == 302


# --- the list --------------------------------------------------------------------------------


class TestList:
    def test_lists_every_unit_with_how_many_materials_have_it(
        self, admin: Client, used: UnitOfMeasure
    ) -> None:
        response = admin.get(reverse("catalog:units"))
        assert "PAL" in listed(response) and "UD" in listed(response)
        rows = {row.code: row.material_count for row in response.context["listing"].rows}
        assert rows["PAL"] == 1 and rows["UD"] == 0

    def test_filters_and_partial(
        self, admin: Client, pallet: UnitOfMeasure, admin_principal: Any
    ) -> None:
        services.update_unit(
            admin_principal, UnitOfMeasure.objects.get(code="BOLSA").pk, is_active=False
        )
        url = reverse("catalog:units")
        assert listed(admin.get(url, {"state": "inactive"})) == ["BOLSA"]
        assert "BOLSA" not in listed(admin.get(url, {"state": "active"}))
        assert listed(admin.get(url, {"q": "palet"})) == ["PAL"]
        assert listed(admin.get(url, {"q": "metro"})) == ["M", "M2"]
        partial = admin.get(url, {"q": "palet"}, headers=HTMX)
        assert partial.content.lstrip().startswith(b'<div id="unit-results">')

    def test_a_row_offers_what_it_may_do(
        self, admin: Client, pallet: UnitOfMeasure, admin_principal: Any
    ) -> None:
        page = admin.get(reverse("catalog:units"), {"q": "palet"}).content.decode()
        assert reverse("catalog:edit_unit", args=[pallet.pk]) in page
        assert reverse("catalog:deactivate_unit", args=[pallet.pk]) in page
        assert reverse("catalog:reactivate_unit", args=[pallet.pk]) not in page
        services.update_unit(admin_principal, pallet.pk, is_active=False)
        again = admin.get(reverse("catalog:units"), {"q": "palet"}).content.decode()
        assert reverse("catalog:reactivate_unit", args=[pallet.pk]) in again
        assert reverse("catalog:deactivate_unit", args=[pallet.pk]) not in again

    def test_the_menu_entry_is_the_current_one(self, admin: Client) -> None:
        page = admin.get(reverse("catalog:units")).content.decode()
        assert 'aria-current="page"' in page


# --- create ----------------------------------------------------------------------------------


class TestCreate:
    def test_the_lazy_dialog(self, admin: Client) -> None:
        url = reverse("catalog:new_unit")
        fragment = admin.get(url, headers=in_dialog("new-dialog"))
        assert fragment.status_code == 200 and b"<html" not in fragment.content
        assert b'data-testid="unit-new-form"' in fragment.content
        whole = admin.get(url)
        assert b"<html" in whole.content and b'data-testid="unit-new-form"' in whole.content

    def test_creates_it(self, people: People, admin: Client) -> None:
        response = admin.post(
            reverse("catalog:create_unit"),
            {"code": " cm ", "name": "Centímetro", "decimals": "2", "idempotency_key": key(people)},
            headers=HTMX,
        )
        made = UnitOfMeasure.objects.get(code="CM")
        assert response.status_code == 204 and response["HX-Redirect"] == reverse("catalog:units")
        assert (made.name, made.decimals, made.is_active) == ("Centímetro", 2, True)
        event = AuditEvent.objects.get(action="unit.created")
        assert event.actor_id == people.group_admin.pk

    def test_a_second_tap_creates_once(self, people: People, admin: Client) -> None:
        data = {"code": "CM", "name": "Centímetro", "decimals": "2", "idempotency_key": key(people)}
        first = admin.post(reverse("catalog:create_unit"), data)
        second = admin.post(reverse("catalog:create_unit"), data)
        assert first.status_code == second.status_code == 302
        assert UnitOfMeasure.objects.filter(code="CM").count() == 1

    def test_a_used_code_comes_back_on_the_form(self, people: People, admin: Client) -> None:
        response = admin.post(
            reverse("catalog:create_unit"),
            {"code": "ud", "name": "Again", "decimals": "0", "idempotency_key": key(people)},
            headers=HTMX,
        )
        assert response.status_code == 422 and codes(response) == {"unique"}

    def test_too_many_decimals_never_reach_the_service(self, people: People, admin: Client) -> None:
        response = admin.post(
            reverse("catalog:create_unit"),
            {"code": "CM", "name": "Cm", "decimals": "4", "idempotency_key": key(people)},
            headers=HTMX,
        )
        assert response.status_code == 422
        assert response.context["d"].form.has_error("decimals")
        assert not UnitOfMeasure.objects.filter(code="CM").exists()

    def test_an_empty_form_is_shown_again(self, admin: Client) -> None:
        assert admin.post(reverse("catalog:create_unit"), headers=HTMX).status_code == 422
        plain = admin.post(reverse("catalog:create_unit"))
        assert plain.status_code == 302 and plain["Location"] == reverse("catalog:units")


# --- edit ------------------------------------------------------------------------------------


class TestEdit:
    def test_a_free_unit_edits_every_field(self, admin: Client, pallet: UnitOfMeasure) -> None:
        response = admin.get(
            reverse("catalog:edit_unit", args=[pallet.pk]), headers=in_dialog("edit-dialog")
        )
        form = response.context["d"].form
        assert not form.fields["code"].disabled
        assert form.initial == {"code": "PAL", "name": "Palet", "decimals": 1}

    def test_a_unit_in_use_shows_its_code_fixed_and_its_decimals_raise_only(
        self, admin: Client, used: UnitOfMeasure
    ) -> None:
        response = admin.get(
            reverse("catalog:edit_unit", args=[used.pk]), headers=in_dialog("edit-dialog")
        )
        form = response.context["d"].form
        assert form.fields["code"].disabled
        assert 'min="1"' in str(form["decimals"])
        assert "keep their quantities" in response.content.decode() or response.context["d"].note

    def test_saves_the_data(self, people: People, admin: Client, pallet: UnitOfMeasure) -> None:
        response = admin.post(
            reverse("catalog:update_unit", args=[pallet.pk]),
            {"code": "PLT", "name": "Palé", "decimals": "0"},
            headers=HTMX,
        )
        assert response.status_code == 204
        pallet.refresh_from_db()
        assert (pallet.code, pallet.name, pallet.decimals) == ("PLT", "Palé", 0)
        event = AuditEvent.objects.get(action="unit.updated")
        assert event.actor_id == people.group_admin.pk

    def test_the_code_of_a_unit_in_use_ignores_what_is_posted(
        self, admin: Client, used: UnitOfMeasure
    ) -> None:
        response = admin.post(
            reverse("catalog:update_unit", args=[used.pk]),
            {"code": "HACK", "name": "Renamed", "decimals": "1"},
            headers=HTMX,
        )
        assert response.status_code == 204
        used.refresh_from_db()
        assert (used.code, used.name) == ("PAL", "Renamed")

    def test_lowering_the_decimals_of_a_unit_in_use_is_refused_on_the_form(
        self, admin: Client, used: UnitOfMeasure
    ) -> None:
        response = admin.post(
            reverse("catalog:update_unit", args=[used.pk]),
            {"code": "PAL", "name": "Palet", "decimals": "0"},
            headers=HTMX,
        )
        assert response.status_code == 422
        assert response.context["d"].form.has_error("decimals")
        assert UnitOfMeasure.objects.get(pk=used.pk).decimals == 1

    def test_the_service_decides_when_the_form_was_built_before_the_unit_was_used(
        self,
        admin: Client,
        admin_principal: Any,
        pallet: UnitOfMeasure,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A material is added between the dialog and the submit: the page's reading said
        "free", the service says no, and its reason reaches the dialog."""
        services.create_material(admin_principal, reference="M-1", name="T", unit_id=pallet.pk)
        monkeypatch.setattr("apps.catalog.unit_views.selectors.unit_in_use", lambda unit: False)
        response = admin.post(
            reverse("catalog:update_unit", args=[pallet.pk]),
            {"code": "PAL", "name": "Palet", "decimals": "0"},
            headers=HTMX,
        )
        assert response.status_code == 422 and codes(response) == {"in_use"}
        assert UnitOfMeasure.objects.get(pk=pallet.pk).decimals == 1
        plain = admin.post(
            reverse("catalog:update_unit", args=[pallet.pk]),
            {"code": "PAL", "name": "Palet", "decimals": "0"},
        )
        assert plain.status_code == 302 and plain["Location"] == reverse("catalog:units")

    def test_an_invalid_form_is_shown_again(self, admin: Client, pallet: UnitOfMeasure) -> None:
        url = reverse("catalog:update_unit", args=[pallet.pk])
        assert admin.post(url, headers=HTMX).status_code == 422
        assert admin.post(url)["Location"] == reverse("catalog:units")


# --- activity --------------------------------------------------------------------------------


class TestActivity:
    def test_deactivates_and_reactivates(
        self, people: People, admin: Client, used: UnitOfMeasure
    ) -> None:
        response = admin.post(reverse("catalog:deactivate_unit", args=[used.pk]))
        assert response.status_code == 302 and response["Location"] == reverse("catalog:units")
        used.refresh_from_db()
        assert not used.is_active
        assert Material.objects.get(reference="M-1").unit_id == used.pk  # it keeps it
        again = admin.post(reverse("catalog:reactivate_unit", args=[used.pk]), headers=HTMX)
        assert again.status_code == 204
        assert UnitOfMeasure.objects.get(pk=used.pk).is_active
        events = AuditEvent.objects.filter(action="unit.updated")
        assert events.count() == 2 and {e.actor_id for e in events} == {people.group_admin.pk}

    def test_the_flash_names_the_unit(self, admin: Client, pallet: UnitOfMeasure) -> None:
        admin.post(reverse("catalog:deactivate_unit", args=[pallet.pk]))
        messages = [str(m) for m in admin.get(reverse("catalog:units")).context["messages"]]
        assert any("PAL" in message for message in messages)


# --- the choices of a material ---------------------------------------------------------------


class TestMaterialChoices:
    def test_an_inactive_unit_leaves_the_choices_for_a_new_material(
        self, org: Org, people: People, admin_principal: Any, pallet: UnitOfMeasure
    ) -> None:
        services.update_unit(admin_principal, pallet.pk, is_active=False)
        client = client_for(people.mm_mad)
        form = client.get(reverse("catalog:new")).context["form"]
        offered = {unit.code for unit in form.fields["unit"].queryset}
        assert "PAL" not in offered and "UD" in offered
        response = client.post(
            reverse("catalog:create"),
            {
                "reference": "NEW-1",
                "name": "New",
                "unit": str(pallet.pk),
                "idempotency_key": issue_key(people.mm_mad.pk, "material.create"),
            },
        )
        assert response.status_code == 200 and response.context["form"].has_error("unit")
        assert not Material.objects.filter(reference="NEW-1").exists()

    def test_a_material_that_has_it_keeps_it_when_edited(
        self, org: Org, people: People, admin_principal: Any, used: UnitOfMeasure
    ) -> None:
        services.update_unit(admin_principal, used.pk, is_active=False)
        material = Material.objects.get(reference="M-1")
        client = client_for(people.mm_mad)
        form = client.get(reverse("catalog:edit", args=[material.pk])).context["form"]
        assert "PAL" in {unit.code for unit in form.fields["unit"].queryset}
        response = client.post(
            reverse("catalog:update", args=[material.pk]),
            {"name": "Renamed", "unit": str(used.pk)},
        )
        assert response.status_code == 302
        material.refresh_from_db()
        assert (material.name, material.unit_id) == ("Renamed", used.pk)

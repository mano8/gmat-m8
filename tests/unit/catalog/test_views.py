"""Catalog screens (M3.1): list, search, detail, create, edit, deactivate and categories."""

import re
from typing import Any

import pytest
from django.urls import reverse

from apps.audit.models import AuditEvent
from apps.catalog import services
from apps.catalog.models import Material, MaterialCategory, UnitOfMeasure
from apps.core.idempotency import issue_key
from tests.org import Org, People, client_for

pytestmark = pytest.mark.django_db


def references(response: Any) -> list[str]:
    return re.findall(r'href="/catalog/[0-9a-f-]{36}/">([^<]+)</a>', response.content.decode())


def form_key(response: Any) -> str:
    match = re.search(r'name="idempotency_key" value="([^"]+)"', response.content.decode())
    assert match is not None
    return match.group(1)


@pytest.fixture
def ud() -> UnitOfMeasure:
    return UnitOfMeasure.objects.get(code="UD")


@pytest.fixture
def materials(admin_principal: Any, ud: UnitOfMeasure) -> dict[str, Material]:
    cables = services.create_category(admin_principal, name="Cables")
    return {
        "cable": services.create_material(
            admin_principal,
            reference="CAB-25",
            name="Cable H07V-K 1x2,5 mm²",
            unit_id=ud.pk,
            category_id=cables.pk,
        ),
        "diff": services.create_material(
            admin_principal, reference="DIF-40", name="Diferencial 40A", unit_id=ud.pk
        ),
    }


class TestRead:
    def test_every_role_reads_the_catalog(self, org: Org, people: People, materials: Any) -> None:
        for name in ("group_admin", "mm_mad", "wo_mad", "tech_mad", "site_manager", "partner"):
            client = client_for(getattr(people, name))
            listing = client.get(reverse("catalog:list"))
            assert listing.status_code == 200, name
            assert references(listing) == ["CAB-25", "DIF-40"], name
            detail = client.get(reverse("catalog:detail", args=[materials["cable"].pk]))
            assert detail.status_code == 200, name

    def test_only_managers_get_the_buttons(self, org: Org, people: People, materials: Any) -> None:
        url = reverse("catalog:detail", args=[materials["cable"].pk])
        for name, shown in (("mm_mad", True), ("group_admin", True), ("tech_mad", False)):
            page = client_for(getattr(people, name)).get(url).content.decode()
            assert (reverse("catalog:edit", args=[materials["cable"].pk]) in page) is shown, name
            assert (reverse("catalog:deactivate", args=[materials["cable"].pk]) in page) is shown
        listing = client_for(people.tech_mad).get(reverse("catalog:list")).content.decode()
        assert reverse("catalog:new") not in listing

    def test_filters_and_the_htmx_partial(self, org: Org, people: People, materials: Any) -> None:
        client = client_for(people.tech_mad)
        assert references(client.get(reverse("catalog:list"), {"q": "diferencial"})) == ["DIF-40"]
        category = materials["cable"].category
        assert references(client.get(reverse("catalog:list"), {"category": category.pk})) == [
            "CAB-25"
        ]
        partial = client.get(reverse("catalog:list"), {"q": "cable 2,5"}, HTTP_HX_REQUEST="true")
        assert partial.content.lstrip().startswith(b'<div id="material-results">')
        assert b"<html" not in partial.content
        assert references(partial) == ["CAB-25"]
        empty = client.get(reverse("catalog:list"), {"q": "nothing-matches"})
        assert references(empty) == []

    def test_inactive_materials_are_listed_on_request(
        self, org: Org, people: People, admin_principal: Any, materials: Any
    ) -> None:
        services.deactivate_material(admin_principal, materials["diff"].pk)
        client = client_for(people.tech_mad)
        assert references(client.get(reverse("catalog:list"))) == ["CAB-25"]
        assert references(client.get(reverse("catalog:list"), {"state": "inactive"})) == ["DIF-40"]
        assert references(client.get(reverse("catalog:list"), {"state": "all"})) == [
            "CAB-25",
            "DIF-40",
        ]
        detail = client.get(reverse("catalog:detail", args=[materials["diff"].pk]))
        assert detail.status_code == 200  # documents keep pointing at inactive materials

    def test_the_search_fragment(self, org: Org, people: People, materials: Any) -> None:
        client = client_for(people.partner)
        response = client.get(reverse("catalog:search"), {"q": "DIFERENCIAL"})
        assert response.status_code == 200
        body = response.content.decode()
        assert "DIF-40" in body and "CAB-25" not in body
        assert "<html" not in body
        nothing = client.get(reverse("catalog:search"), {"q": "zzz"}).content.decode()
        assert "text-soft" in nothing and 'role="option"' not in nothing  # the empty message
        blank = client.get(reverse("catalog:search"))  # no query: an empty list, no message
        assert blank.status_code == 200 and "text-soft" not in blank.content.decode()

    def test_a_malformed_filter_is_ignored(self, org: Org, people: People, materials: Any) -> None:
        response = client_for(people.tech_mad).get(
            reverse("catalog:list"), {"category": "not-an-id", "state": "weird", "page": "x"}
        )
        assert response.status_code == 200
        assert references(response) == ["CAB-25", "DIF-40"]

    def test_unknown_materials_are_404(self, org: Org, people: People) -> None:
        missing = reverse("catalog:detail", args=["00000000-0000-7000-8000-000000000000"])
        assert client_for(people.tech_mad).get(missing).status_code == 404


class TestWrite:
    def test_create(self, org: Org, people: People, ud: UnitOfMeasure) -> None:
        client = client_for(people.mm_mad)
        key = form_key(client.get(reverse("catalog:new")))
        data = {
            "reference": "MAG-16",
            "name": "Magnetotérmico 16A",
            "unit": ud.pk,
            "category": "",
            "ean": "",
            "is_stock_item": "on",
            "idempotency_key": key,
        }
        response = client.post(reverse("catalog:create"), data)
        material = Material.objects.get(reference="MAG-16")
        assert response.status_code == 302
        assert response["Location"] == reverse("catalog:detail", args=[material.pk])
        assert material.is_stock_item and material.is_active
        again = client.post(reverse("catalog:create"), data)  # a double click
        assert again["Location"] == response["Location"]
        assert Material.objects.filter(reference="MAG-16").count() == 1

    def test_create_errors_stay_on_the_form(
        self, org: Org, people: People, ud: UnitOfMeasure, materials: Any
    ) -> None:
        client = client_for(people.mm_mad)
        key = issue_key(people.mm_mad.pk, "material.create")
        duplicate = client.post(
            reverse("catalog:create"),
            {"reference": "cab-25", "name": "Again", "unit": ud.pk, "idempotency_key": key},
        )
        assert duplicate.status_code == 200 and b"errorlist" in duplicate.content
        bad_ean = client.post(
            reverse("catalog:create"),
            {"reference": "X", "name": "X", "unit": ud.pk, "ean": "12", "idempotency_key": key},
        )
        assert bad_ean.status_code == 200 and b"errorlist" in bad_ean.content
        assert Material.objects.count() == 2
        foreign_key = client.post(
            reverse("catalog:create"),
            {
                "reference": "X",
                "name": "X",
                "unit": ud.pk,
                "idempotency_key": issue_key(people.mm_sev.pk, "material.create"),
            },
        )
        assert foreign_key.status_code == 400  # a key of another user

    def test_edit_and_the_reference_is_fixed(
        self, org: Org, people: People, ud: UnitOfMeasure, materials: Any
    ) -> None:
        client = client_for(people.mm_mad)
        material = materials["cable"]
        page = client.get(reverse("catalog:edit", args=[material.pk]))
        assert page.status_code == 200 and b'name="reference"' not in page.content
        response = client.post(
            reverse("catalog:update", args=[material.pk]),
            {"name": "Cable 2,5", "unit": ud.pk, "manufacturer": "Prysmian", "reference": "HACK"},
        )
        assert response.status_code == 302
        material.refresh_from_db()
        assert (material.reference, material.name, material.manufacturer) == (
            "CAB-25",
            "Cable 2,5",
            "Prysmian",
        )
        assert material.category_id is None  # cleared by the form: it was left empty

    def test_deactivate_and_reactivate(self, org: Org, people: People, materials: Any) -> None:
        client = client_for(people.mm_mad)
        material = materials["cable"]
        assert client.post(reverse("catalog:deactivate", args=[material.pk])).status_code == 302
        material.refresh_from_db()
        assert not material.is_active
        assert client.post(reverse("catalog:reactivate", args=[material.pk])).status_code == 302
        material.refresh_from_db()
        assert material.is_active
        assert AuditEvent.objects.filter(action="material.deactivated").count() == 1

    def test_non_managers_cannot_write(self, org: Org, people: People, materials: Any) -> None:
        material = materials["cable"]
        for name in ("wo_mad", "tech_mad", "site_manager", "partner"):
            client = client_for(getattr(people, name))
            assert client.get(reverse("catalog:new")).status_code == 403, name
            assert client.post(reverse("catalog:create")).status_code == 403, name
            assert client.get(reverse("catalog:edit", args=[material.pk])).status_code == 403
            assert client.post(reverse("catalog:update", args=[material.pk])).status_code == 403
            assert client.post(reverse("catalog:deactivate", args=[material.pk])).status_code == 403
        material.refresh_from_db()
        assert material.is_active

    def test_get_never_changes_anything(self, org: Org, people: People, materials: Any) -> None:
        client = client_for(people.mm_mad)
        material = materials["cable"]
        for name in ("deactivate", "reactivate", "update"):
            assert client.get(reverse(f"catalog:{name}", args=[material.pk])).status_code == 405
        assert client.get(reverse("catalog:create")).status_code == 405


class TestCategories:
    def test_list_for_everyone_manage_for_managers(
        self, org: Org, people: People, materials: Any
    ) -> None:
        category = materials["cable"].category
        edit = reverse("catalog:category_edit", args=[category.pk])
        assert edit in client_for(people.mm_mad).get(reverse("catalog:categories")).content.decode()
        page = client_for(people.tech_mad).get(reverse("catalog:categories"))
        assert page.status_code == 200 and edit not in page.content.decode()
        assert b"Cables" in page.content

    def test_create_edit_and_the_two_level_rule(
        self, org: Org, people: People, materials: Any
    ) -> None:
        client = client_for(people.mm_mad)
        root = materials["cable"].category
        key = form_key(client.get(reverse("catalog:category_new")))
        created = client.post(
            reverse("catalog:category_create"),
            {"name": "Unipolares", "parent": root.pk, "idempotency_key": key},
        )
        assert created.status_code == 302
        child = MaterialCategory.objects.get(name="Unipolares")
        assert child.parent_id == root.pk
        deep = client.post(
            reverse("catalog:category_create"),
            {
                "name": "Libres",
                "parent": child.pk,  # not offered by the form: refused
                "idempotency_key": issue_key(people.mm_mad.pk, "category.create"),
            },
        )
        assert deep.status_code == 200 and b"errorlist" in deep.content
        edit = client.get(reverse("catalog:category_edit", args=[child.pk]))
        assert edit.status_code == 200
        renamed = client.post(
            reverse("catalog:category_update", args=[child.pk]),
            {"name": "Rígidos", "parent": root.pk},
        )
        assert renamed.status_code == 302
        child.refresh_from_db()
        assert child.name == "Rígidos"
        moved = client.post(
            reverse("catalog:category_update", args=[root.pk]),
            {"name": "Cables", "parent": child.pk},  # a root with children, under its own child
        )
        assert moved.status_code == 200 and b"errorlist" in moved.content

    def test_non_managers_cannot_write(self, org: Org, people: People, materials: Any) -> None:
        category = materials["cable"].category
        client = client_for(people.tech_mad)
        assert client.get(reverse("catalog:category_new")).status_code == 403
        assert client.post(reverse("catalog:category_create")).status_code == 403
        assert client.get(reverse("catalog:category_edit", args=[category.pk])).status_code == 403
        assert (
            client.post(reverse("catalog:category_update", args=[category.pk])).status_code == 403
        )

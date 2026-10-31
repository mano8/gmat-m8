"""Supplier screens and reads (M3.2): who sees suppliers and who sees nothing."""

import re
from typing import Any

import pytest
from django.urls import reverse

from apps.access.context import AccessContext
from apps.access.exceptions import NotInScope
from apps.audit.models import AuditEvent
from apps.core.idempotency import issue_key
from apps.suppliers import selectors, services
from apps.suppliers.models import Supplier
from tests.org import Org, People, Sentinel, client_for

pytestmark = pytest.mark.django_db

SEES = ("group_admin", "mm_mad", "mm_sev", "wo_mad", "mixed")
SEES_NOTHING = ("tech_mad", "site_manager", "partner", "revoked_assignment")


def names(response: Any) -> list[str]:
    return re.findall(r'href="/suppliers/[0-9a-f-]{36}/">([^<]+)</a>', response.content.decode())


def form_key(response: Any) -> str:
    match = re.search(r'name="idempotency_key" value="([^"]+)"', response.content.decode())
    assert match is not None
    return match.group(1)


class TestAccess:
    def test_viewers_read_every_supplier(self, org: Org, people: People) -> None:
        for who in SEES:
            client = client_for(getattr(people, who))
            listing = client.get(reverse("suppliers:list"))
            assert names(listing) == [Sentinel.SUPPLIER_NAME.value], who
            detail = client.get(reverse("suppliers:detail", args=[org.supplier.pk]))
            body = detail.content.decode()
            assert Sentinel.SUPPLIER_EMAIL.value in body, who
            assert Sentinel.SUPPLIER_PHONE.value in body, who

    def test_everyone_else_gets_nothing_on_any_url(self, org: Org, people: People) -> None:
        """No supplier name, email or phone reaches a site manager, technician or partner."""
        pk = org.supplier.pk
        urls = [
            ("get", reverse("suppliers:list")),
            ("get", reverse("suppliers:detail", args=[pk])),
            ("get", reverse("suppliers:new")),
            ("get", reverse("suppliers:edit", args=[pk])),
            ("post", reverse("suppliers:create")),
            ("post", reverse("suppliers:update", args=[pk])),
            ("post", reverse("suppliers:deactivate", args=[pk])),
        ]
        for who in SEES_NOTHING:
            client = client_for(getattr(people, who))
            for method, url in urls:
                response = getattr(client, method)(url)
                assert response.status_code == 403, (who, url)
                assert not any(s.found_in(response.content.decode()) for s in Sentinel)

    def test_the_navigation_entry_follows_the_capability(self, org: Org, people: People) -> None:
        shown = reverse("suppliers:list").encode()
        assert shown in client_for(people.wo_mad).get(reverse("catalog:list")).content
        assert shown not in client_for(people.tech_mad).get(reverse("catalog:list")).content

    def test_only_managers_get_the_buttons(self, org: Org, people: People) -> None:
        pk = org.supplier.pk
        edit = reverse("suppliers:edit", args=[pk]).encode()
        assert edit in client_for(people.mm_mad).get(reverse("suppliers:detail", args=[pk])).content
        warehouse = client_for(people.wo_mad).get(reverse("suppliers:detail", args=[pk]))
        assert warehouse.status_code == 200 and edit not in warehouse.content
        assert reverse("suppliers:new").encode() not in (
            client_for(people.wo_mad).get(reverse("suppliers:list")).content
        )

    def test_selectors_return_nothing_without_the_capability(
        self, org: Org, people: People
    ) -> None:
        for who in SEES_NOTHING:
            ctx = AccessContext.for_user(getattr(people, who))
            assert list(selectors.suppliers(ctx, state=selectors.STATE_ALL)) == []
            with pytest.raises(NotInScope):
                selectors.supplier(ctx, org.supplier.pk)
            assert not selectors.manageable(ctx)


class TestScreens:
    @pytest.fixture
    def suppliers(self, admin_principal: Any) -> dict[str, Supplier]:
        return {
            "acme": services.create_supplier(
                admin_principal, name="Acme Eléctrica", tax_id="B1", contact_name="Marta"
            ),
            "zeta": services.create_supplier(admin_principal, name="Zeta Cables", tax_id="B2"),
        }

    def test_filters_states_and_the_htmx_partial(
        self, org: Org, people: People, suppliers: Any, admin_principal: Any
    ) -> None:
        services.deactivate_supplier(admin_principal, suppliers["zeta"].pk)
        client = client_for(people.mm_mad)
        assert names(client.get(reverse("suppliers:list"))) == [
            "Acme Eléctrica",
            Sentinel.SUPPLIER_NAME.value,
        ]
        assert names(client.get(reverse("suppliers:list"), {"state": "inactive"})) == [
            "Zeta Cables"
        ]
        assert len(names(client.get(reverse("suppliers:list"), {"state": "all"}))) == 3
        assert names(client.get(reverse("suppliers:list"), {"q": "marta"})) == ["Acme Eléctrica"]
        assert names(client.get(reverse("suppliers:list"), {"q": "b1"})) == ["Acme Eléctrica"]
        partial = client.get(reverse("suppliers:list"), {"q": "acme"}, HTTP_HX_REQUEST="true")
        assert partial.content.lstrip().startswith(b'<div id="supplier-results">')
        assert b"<html" not in partial.content
        assert names(client.get(reverse("suppliers:list"), {"q": "nothing"})) == []

    def test_create_edit_and_deactivate(self, org: Org, people: People) -> None:
        client = client_for(people.mm_mad)
        key = form_key(client.get(reverse("suppliers:new")))
        data = {"name": "Nuevo", "tax_id": "B9", "email": "n@x.example", "idempotency_key": key}
        response = client.post(reverse("suppliers:create"), data)
        supplier = Supplier.objects.get(name="Nuevo")
        assert response["Location"] == reverse("suppliers:detail", args=[supplier.pk])
        again = client.post(reverse("suppliers:create"), data)  # a double click
        assert (
            again["Location"] == response["Location"]
            and Supplier.objects.filter(name="Nuevo").count() == 1
        )
        edit = client.get(reverse("suppliers:edit", args=[supplier.pk]))
        assert edit.status_code == 200 and b"Nuevo" in edit.content
        saved = client.post(
            reverse("suppliers:update", args=[supplier.pk]),
            {"name": "Nuevo SL", "tax_id": "B9", "phone": "600"},
        )
        assert saved.status_code == 302
        supplier.refresh_from_db()
        assert (supplier.name, supplier.phone, supplier.email) == ("Nuevo SL", "600", "")
        assert client.post(reverse("suppliers:deactivate", args=[supplier.pk])).status_code == 302
        supplier.refresh_from_db()
        assert not supplier.is_active
        assert client.post(reverse("suppliers:reactivate", args=[supplier.pk])).status_code == 302
        assert AuditEvent.objects.filter(action="supplier.reactivated").exists()

    def test_errors_stay_on_the_form(self, org: Org, people: People) -> None:
        client = client_for(people.mm_mad)
        key = issue_key(people.mm_mad.pk, "supplier.create")
        duplicate = client.post(
            reverse("suppliers:create"),
            {"name": Sentinel.SUPPLIER_NAME.value.upper(), "idempotency_key": key},
        )
        assert duplicate.status_code == 200 and b"errorlist" in duplicate.content
        bad_email = client.post(
            reverse("suppliers:create"), {"name": "X", "email": "nope", "idempotency_key": key}
        )
        assert bad_email.status_code == 200 and b"errorlist" in bad_email.content
        foreign = client.post(
            reverse("suppliers:create"),
            {"name": "X", "idempotency_key": issue_key(people.mm_sev.pk, "supplier.create")},
        )
        assert foreign.status_code == 400
        assert Supplier.objects.count() == 1

    def test_get_never_changes_anything(self, org: Org, people: People) -> None:
        client = client_for(people.mm_mad)
        for name in ("deactivate", "reactivate", "update"):
            assert (
                client.get(reverse(f"suppliers:{name}", args=[org.supplier.pk])).status_code == 405
            )
        assert client.get(reverse("suppliers:create")).status_code == 405

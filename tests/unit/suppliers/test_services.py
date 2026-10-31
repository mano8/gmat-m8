"""Supplier services (plan §4.2, M3.2): roles, invariants, audit and idempotency."""

from typing import Any

import pytest
from django.core.exceptions import ValidationError

from apps.access.exceptions import AccessDenied, MFARequired, NotInScope
from apps.access.principals import SessionPrincipal
from apps.audit.models import AuditEvent
from apps.core.idempotency import IdempotencyConflict, issue_key
from apps.suppliers import services
from apps.suppliers.models import Supplier
from tests.org import Org
from tests.support import principal_for

pytestmark = pytest.mark.django_db


@pytest.fixture
def acme(admin_principal: SessionPrincipal) -> Supplier:
    return services.create_supplier(
        admin_principal, name="Acme Eléctrica", tax_id="B12345678", email="Ventas@Acme.example"
    )


class TestSuppliers:
    def test_create_normalizes_and_audits(self, acme: Supplier) -> None:
        assert (acme.name, acme.tax_id, acme.email) == (
            "Acme Eléctrica",
            "B12345678",
            "ventas@acme.example",
        )
        assert acme.is_active
        event = AuditEvent.objects.get(action="supplier.created", object_id=acme.pk)
        assert event.data == {"name": "Acme Eléctrica", "tax_id": "B12345678"}

    def test_a_blank_tax_id_is_none_and_many_suppliers_may_have_none(
        self, admin_principal: SessionPrincipal
    ) -> None:
        first = services.create_supplier(admin_principal, name="One", tax_id="  ")
        second = services.create_supplier(admin_principal, name="Two")
        assert first.tax_id is None and second.tax_id is None

    @pytest.mark.parametrize(
        ("values", "field"),
        [
            ({"name": "  "}, "name"),
            ({"email": "not-an-email"}, "email"),
            ({"name": "acme eléctrica"}, "name"),  # unique ignoring case
            ({"name": "Other", "tax_id": "B12345678"}, "tax_id"),
        ],
    )
    def test_invariants(
        self, admin_principal: SessionPrincipal, acme: Supplier, values: dict[str, Any], field: str
    ) -> None:
        with pytest.raises(ValidationError) as exc:
            services.create_supplier(admin_principal, **({"name": "New"} | values))
        assert field in exc.value.error_dict
        assert Supplier.objects.count() == 1

    def test_update_audits_the_diff_and_a_noop_writes_nothing(
        self, admin_principal: SessionPrincipal, acme: Supplier
    ) -> None:
        updated = services.update_supplier(
            admin_principal, acme.pk, phone=" 600 000 000 ", notes="Pays in 30 days"
        )
        assert (updated.phone, updated.notes) == ("600 000 000", "Pays in 30 days")
        event = AuditEvent.objects.get(action="supplier.updated", object_id=acme.pk)
        assert event.data == {"fields": ["notes", "phone"]}
        events = AuditEvent.objects.count()
        services.update_supplier(admin_principal, acme.pk, phone="600 000 000", tax_id="B12345678")
        assert AuditEvent.objects.count() == events
        with pytest.raises(ValueError, match="cannot be changed"):
            services.update_supplier(admin_principal, acme.pk, is_active=False)

    def test_update_cannot_take_another_suppliers_key(
        self, admin_principal: SessionPrincipal, acme: Supplier
    ) -> None:
        other = services.create_supplier(admin_principal, name="Other", tax_id="B87654321")
        with pytest.raises(ValidationError) as exc:
            services.update_supplier(admin_principal, other.pk, tax_id="B12345678")
        assert "tax_id" in exc.value.error_dict

    def test_deactivate_and_reactivate(
        self, admin_principal: SessionPrincipal, acme: Supplier
    ) -> None:
        assert not services.deactivate_supplier(admin_principal, acme.pk).is_active
        services.deactivate_supplier(admin_principal, acme.pk)
        assert AuditEvent.objects.filter(action="supplier.deactivated").count() == 1
        assert services.reactivate_supplier(admin_principal, acme.pk).is_active
        assert AuditEvent.objects.filter(action="supplier.reactivated").count() == 1

    def test_a_replayed_create_returns_the_first_result(
        self, admin_principal: SessionPrincipal
    ) -> None:
        key = issue_key(admin_principal.user_id, "supplier.create")
        first = services.create_supplier(admin_principal, name="Once", idempotency_key=key)
        again = services.create_supplier(admin_principal, name="Once", idempotency_key=key)
        assert again.pk == first.pk and Supplier.objects.count() == 1
        with pytest.raises(IdempotencyConflict):
            services.create_supplier(admin_principal, name="Twice", idempotency_key=key)


class TestAuthorization:
    """``supplier.manage``: group administrators and material managers only."""

    @pytest.mark.parametrize("who", ["group_admin", "mm_mad", "mm_sev", "mixed"])
    def test_allowed(self, org: Org, people: Any, who: str) -> None:
        supplier = services.create_supplier(principal_for(getattr(people, who)), name=f"S-{who}")
        assert Supplier.objects.filter(pk=supplier.pk).exists()

    @pytest.mark.parametrize(
        "who",
        [
            "wo_mad",  # can view suppliers, not change them
            "tech_mad",
            "site_manager",
            "partner",
            "expired",
            "revoked",
            "revoked_assignment",
            "inactive_partner",
            "inactive_branch",
            "deactivated",
            "nobody",
            "operator",
        ],
    )
    def test_denied(self, org: Org, people: Any, who: str) -> None:
        principal = principal_for(getattr(people, who))
        events = AuditEvent.objects.count()
        sentinel = org.supplier
        for call in (
            lambda: services.create_supplier(principal, name="X"),
            lambda: services.update_supplier(principal, sentinel.pk, notes="Hijacked"),
            lambda: services.deactivate_supplier(principal, sentinel.pk),
        ):
            with pytest.raises((AccessDenied, NotInScope)):
                call()
        assert AuditEvent.objects.count() == events
        sentinel.refresh_from_db()
        assert sentinel.is_active and sentinel.notes != "Hijacked"

    def test_a_warehouse_operator_is_denied_by_capability_not_by_scope(
        self, org: Org, people: Any
    ) -> None:
        principal = principal_for(people.wo_mad)
        with pytest.raises(AccessDenied):
            services.update_supplier(principal, org.supplier.pk, notes="x")  # sees it: 403
        with pytest.raises(NotInScope):
            services.update_supplier(principal_for(people.partner), org.supplier.pk, notes="x")

    def test_a_privileged_role_needs_session_mfa(self, org: Org, people: Any) -> None:
        with pytest.raises(MFARequired):
            services.create_supplier(principal_for(people.mm_mad, mfa_at=None), name="X")


def test_create_supplier_refuses_an_unknown_argument(admin_principal: SessionPrincipal) -> None:
    with pytest.raises(TypeError, match="unexpected arguments"):
        services.create_supplier(admin_principal, name="X", colour="red")

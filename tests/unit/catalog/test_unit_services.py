"""Units of measure services (M10.3, Q72): create, change, deactivate; the rules that keep the
quantities already entered true. Allowed, denied and revoked actors, audit, idempotency."""

from collections.abc import Callable
from decimal import Decimal
from typing import Any

import pytest
from django.core.exceptions import ValidationError
from django.db.models.fields.reverse_related import ManyToOneRel

from apps.access.exceptions import AccessDenied, MFARequired
from apps.access.principals import SessionPrincipal
from apps.accounts.models import User
from apps.audit.models import AuditEvent
from apps.catalog import selectors, services
from apps.catalog.models import Material, UnitOfMeasure
from apps.core.idempotency import IdempotencyConflict, issue_key
from apps.material_requests.models import MaterialRequest, MaterialRequestLine
from tests.org import Org
from tests.support import error_codes, principal_for

pytestmark = pytest.mark.django_db


@pytest.fixture
def pair(admin_principal: SessionPrincipal) -> UnitOfMeasure:
    return services.create_unit(admin_principal, code="PAL", name="Palet", decimals=1)


@pytest.fixture
def used(admin_principal: SessionPrincipal, pair: UnitOfMeasure) -> UnitOfMeasure:
    """A unit that a material has."""
    services.create_material(admin_principal, reference="M-1", name="Thing", unit_id=pair.pk)
    return pair


class TestCreate:
    def test_a_new_unit_is_active_upper_case_and_audited(
        self, admin_principal: SessionPrincipal
    ) -> None:
        unit = services.create_unit(admin_principal, code=" cm ", name=" Centímetro ", decimals=2)
        assert (unit.code, unit.name, unit.decimals, unit.is_active) == (
            "CM",
            "Centímetro",
            2,
            True,
        )
        event = AuditEvent.objects.get(action="unit.created", object_id=unit.pk)
        assert event.data == {"code": "CM", "name": "Centímetro", "decimals": 2}
        assert event.actor_label == "admin@example.org"

    @pytest.mark.parametrize(
        ("values", "field"),
        [
            ({"code": ""}, "code"),
            ({"code": "TOOLONGCODE1"}, "code"),
            ({"name": "  "}, "name"),
            ({"decimals": 4}, "decimals"),
            ({"decimals": -1}, "decimals"),
        ],
    )
    def test_invariants(
        self, admin_principal: SessionPrincipal, values: dict[str, Any], field: str
    ) -> None:
        given: dict[str, Any] = {"code": "XX", "name": "Thing", "decimals": 0} | values
        with pytest.raises(ValidationError) as exc:
            services.create_unit(admin_principal, **given)
        assert field in exc.value.error_dict
        assert not UnitOfMeasure.objects.filter(code="XX").exists()

    @pytest.mark.parametrize("code", ["UD", "ud", " Ud "])
    def test_a_code_is_unique_ignoring_case(
        self, admin_principal: SessionPrincipal, code: str
    ) -> None:
        with pytest.raises(ValidationError) as exc:
            services.create_unit(admin_principal, code=code, name="Again")
        assert error_codes(exc) == {"unique"}

    def test_a_double_tap_creates_one(self, admin_principal: SessionPrincipal) -> None:
        key = issue_key(admin_principal.user_id, "unit.create")
        first = services.create_unit(
            admin_principal, code="CM", name="Centímetro", idempotency_key=key
        )
        again = services.create_unit(
            admin_principal, code="CM", name="Centímetro", idempotency_key=key
        )
        assert again.pk == first.pk
        assert AuditEvent.objects.filter(action="unit.created").count() == 1
        with pytest.raises(IdempotencyConflict):
            services.create_unit(admin_principal, code="MM", name="Milímetro", idempotency_key=key)


class TestUpdate:
    def test_a_unit_nothing_uses_changes_freely(
        self, admin_principal: SessionPrincipal, pair: UnitOfMeasure
    ) -> None:
        saved = services.update_unit(
            admin_principal, pair.pk, code="PLT", name=" Palé ", decimals=0
        )
        assert (saved.code, saved.name, saved.decimals) == ("PLT", "Palé", 0)
        event = AuditEvent.objects.get(action="unit.updated", object_id=pair.pk)
        assert event.data["code"] == {"from": "PAL", "to": "PLT"}
        assert event.data["decimals"] == {"from": 1, "to": 0}

    def test_a_change_that_alters_nothing_writes_nothing(
        self, admin_principal: SessionPrincipal, pair: UnitOfMeasure
    ) -> None:
        events = AuditEvent.objects.count()
        services.update_unit(admin_principal, pair.pk, code="pal", name="Palet", decimals=1)
        assert AuditEvent.objects.count() == events

    def test_a_unit_in_use_keeps_its_code(
        self, admin_principal: SessionPrincipal, used: UnitOfMeasure
    ) -> None:
        with pytest.raises(ValidationError) as exc:
            services.update_unit(admin_principal, used.pk, code="PLT")
        assert error_codes(exc) == {"in_use"}
        assert "code" in exc.value.error_dict
        assert UnitOfMeasure.objects.get(pk=used.pk).code == "PAL"

    def test_a_unit_in_use_never_lowers_its_decimals_but_may_raise_them(
        self, admin_principal: SessionPrincipal, used: UnitOfMeasure
    ) -> None:
        with pytest.raises(ValidationError) as exc:
            services.update_unit(admin_principal, used.pk, decimals=0)
        assert "decimals" in exc.value.error_dict
        assert UnitOfMeasure.objects.get(pk=used.pk).decimals == 1
        assert services.update_unit(admin_principal, used.pk, decimals=3).decimals == 3

    def test_a_refused_change_applies_nothing_of_it(
        self, admin_principal: SessionPrincipal, used: UnitOfMeasure
    ) -> None:
        events = AuditEvent.objects.count()
        with pytest.raises(ValidationError):
            services.update_unit(admin_principal, used.pk, name="Renamed", decimals=0)
        unit = UnitOfMeasure.objects.get(pk=used.pk)
        assert (unit.name, unit.decimals) == ("Palet", 1)
        assert AuditEvent.objects.count() == events

    def test_the_name_changes_even_when_the_unit_is_in_use(
        self, admin_principal: SessionPrincipal, used: UnitOfMeasure
    ) -> None:
        assert services.update_unit(admin_principal, used.pk, name="Pallet").name == "Pallet"

    def test_a_code_cannot_take_another_units_ignoring_case(
        self, admin_principal: SessionPrincipal, pair: UnitOfMeasure
    ) -> None:
        with pytest.raises(ValidationError) as exc:
            services.update_unit(admin_principal, pair.pk, code="ud")
        assert error_codes(exc) == {"unique"}

    def test_an_unknown_field_is_refused(
        self, admin_principal: SessionPrincipal, pair: UnitOfMeasure
    ) -> None:
        with pytest.raises(ValueError, match="id"):
            services.update_unit(admin_principal, pair.pk, id=pair.pk)

    def test_validation_happens_on_update(
        self, admin_principal: SessionPrincipal, pair: UnitOfMeasure
    ) -> None:
        with pytest.raises(ValidationError) as exc:
            services.update_unit(admin_principal, pair.pk, decimals=9)
        assert "decimals" in exc.value.error_dict


class TestActivity:
    def test_deactivate_and_reactivate_keep_what_has_the_unit(
        self, admin_principal: SessionPrincipal, used: UnitOfMeasure
    ) -> None:
        off = services.update_unit(admin_principal, used.pk, is_active=False)
        assert off.is_active is False
        assert Material.objects.get(reference="M-1").unit_id == used.pk
        # An inactive unit still lets its name change, and comes back.
        assert services.update_unit(admin_principal, used.pk, is_active=True).is_active
        changes = [e.data["is_active"] for e in AuditEvent.objects.filter(action="unit.updated")]
        assert sorted(changes, key=lambda c: c["to"]) == [
            {"from": True, "to": False},
            {"from": False, "to": True},
        ]


class TestInUse:
    def test_every_table_that_points_at_a_unit_counts(self) -> None:
        """The guard: ``unit_in_use`` follows the model's reverse relations, so a new table
        that gets a unit is counted without anyone editing it. This pins today's four."""
        models = {
            relation.related_model._meta.label
            for relation in UnitOfMeasure._meta.get_fields(include_hidden=True)
            if isinstance(relation, ManyToOneRel)
        }
        assert models == {
            "catalog.Material",
            "material_requests.MaterialRequestLine",
            "material_requests.MaterialRequestBaselineLine",
            "purchasing.PurchaseOrderLine",
        }

    def test_a_free_text_request_line_counts_as_use(
        self, admin_principal: SessionPrincipal, org: Org, people: Any, pair: UnitOfMeasure
    ) -> None:
        assert not selectors.unit_in_use(pair)
        request = MaterialRequest.objects.create(
            project=org.hospital, procurement_branch=org.a_mad, requested_by=people.tech_mad
        )
        MaterialRequestLine.objects.create(
            request=request,
            project=org.hospital,
            line_no=1,
            free_text="Bandeja",
            unit=pair,
            requested_qty=Decimal("1.500"),
        )
        assert selectors.unit_in_use(pair)
        with pytest.raises(ValidationError):
            services.update_unit(admin_principal, pair.pk, decimals=0)
        with pytest.raises(ValidationError):
            services.update_unit(admin_principal, pair.pk, code="PLT")

    def test_the_seeded_units_are_free_until_used(self) -> None:
        assert not selectors.unit_in_use(UnitOfMeasure.objects.get(code="JUEGO"))


class TestAuthorization:
    """``org.manage``: group administrators only (not even material managers)."""

    @pytest.fixture
    def principals(self, org: Org, people: Any) -> dict[str, SessionPrincipal]:
        return {name: principal_for(getattr(people, name)) for name in people.all()}

    def test_a_group_administrator_is_allowed(
        self, principals: dict[str, SessionPrincipal]
    ) -> None:
        unit = services.create_unit(principals["group_admin"], code="ZZ", name="Zeta")
        assert services.update_unit(principals["group_admin"], unit.pk, name="Z").name == "Z"

    @pytest.mark.parametrize(
        "who",
        [
            "mm_mad",
            "engineer",
            "wo_mad",
            "tech_mad",
            "site_manager",
            "partner",
            "expired",
            "revoked",
            "inactive_partner",
            "deactivated",
            "nobody",
            "operator",
        ],
    )
    def test_everyone_else_is_denied(
        self, principals: dict[str, SessionPrincipal], pair: UnitOfMeasure, who: str
    ) -> None:
        events = AuditEvent.objects.count()
        for call in (
            lambda: services.create_unit(principals[who], code="ZZ", name="Zeta"),
            lambda: services.update_unit(principals[who], pair.pk, name="Hijacked"),
            lambda: services.update_unit(principals[who], pair.pk, is_active=False),
        ):
            with pytest.raises(AccessDenied):
                call()
        assert AuditEvent.objects.count() == events
        assert UnitOfMeasure.objects.get(pk=pair.pk).name == "Palet"

    def test_a_group_administrator_needs_session_mfa(
        self, people: Any, pair: UnitOfMeasure
    ) -> None:
        without_mfa = principal_for(people.group_admin, mfa_at=None)
        with pytest.raises(MFARequired):
            services.update_unit(without_mfa, pair.pk, name="X")

    def test_a_deactivated_account_with_a_live_session_is_refused(
        self, org: Org, people: Any, pair: UnitOfMeasure
    ) -> None:
        principal = principal_for(people.group_admin)
        User.objects.filter(pk=people.group_admin.pk).update(is_active=False)
        with pytest.raises(AccessDenied):
            services.update_unit(principal, pair.pk, name="X")


def test_unit_services_are_transactional() -> None:
    """The services run in the shared wrapper (lock timeout, deadlock retry), like the rest."""
    calls: list[Callable[..., Any]] = [services.create_unit, services.update_unit]
    assert all(hasattr(call, "__wrapped__") for call in calls)

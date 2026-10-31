"""Catalog services (plan §4.2, M3.1): allowed, denied and revoked roles, invariants."""

from collections.abc import Callable
from typing import Any

import pytest
from django.core.exceptions import ValidationError
from django.db import connection

from apps.access.exceptions import AccessDenied, MFARequired, NotInScope
from apps.access.principals import SessionPrincipal
from apps.access.roles import Role
from apps.accounts.models import User
from apps.audit.models import AuditEvent
from apps.catalog import services
from apps.catalog.models import Material, UnitOfMeasure
from apps.core.idempotency import IdempotencyConflict, issue_key
from apps.core.migration_ops import AddCompositeForeignKey
from tests.org import Org
from tests.support import error_codes, principal_for

pytestmark = pytest.mark.django_db


@pytest.fixture
def unit() -> UnitOfMeasure:
    return UnitOfMeasure.objects.get(code="UD")


@pytest.fixture
def cable(admin_principal: SessionPrincipal, unit: UnitOfMeasure) -> Material:
    return services.create_material(
        admin_principal, reference="CAB-2.5", name="Cable H07V-K 1x2,5 mm²", unit_id=unit.pk
    )


class TestUnits:
    def test_the_units_of_the_plan_are_seeded(self) -> None:
        codes = set(UnitOfMeasure.objects.values_list("code", flat=True))
        assert {"UD", "M", "KG", "L", "ROLLO", "CAJA"} <= codes
        assert UnitOfMeasure.objects.get(code="UD").decimals == 0
        assert UnitOfMeasure.objects.get(code="M").decimals == 2


class TestCategories:
    def test_create_and_rename(self, admin_principal: SessionPrincipal) -> None:
        root = services.create_category(admin_principal, name="Cables")
        child = services.create_category(admin_principal, name="Unipolares", parent_id=root.pk)
        assert str(child) == "Cables / Unipolares"
        renamed = services.update_category(admin_principal, child.pk, name=" Rígidos ")
        assert renamed.name == "Rígidos"
        assert AuditEvent.objects.filter(action="category.created").count() == 2
        assert AuditEvent.objects.filter(action="category.updated", object_id=child.pk).exists()

    def test_two_levels_at_most(self, admin_principal: SessionPrincipal) -> None:
        root = services.create_category(admin_principal, name="Cables")
        child = services.create_category(admin_principal, name="Unipolares", parent_id=root.pk)
        with pytest.raises(ValidationError) as deep:
            services.create_category(admin_principal, name="Libres", parent_id=child.pk)
        assert error_codes(deep) == {"depth"}
        other = services.create_category(admin_principal, name="Tubos")
        with pytest.raises(ValidationError) as moved:  # the root has a child already
            services.update_category(admin_principal, root.pk, parent_id=other.pk)
        assert error_codes(moved) == {"depth"}

    def test_names_are_unique_among_siblings_ignoring_case(
        self, admin_principal: SessionPrincipal
    ) -> None:
        root = services.create_category(admin_principal, name="Cables")
        other = services.create_category(admin_principal, name="Tubos")
        with pytest.raises(ValidationError):
            services.create_category(admin_principal, name="CABLES")
        services.create_category(admin_principal, name="Rígidos", parent_id=root.pk)
        services.create_category(admin_principal, name="Rígidos", parent_id=other.pk)
        with pytest.raises(ValidationError):
            services.create_category(admin_principal, name="rígidos", parent_id=root.pk)

    def test_a_noop_update_writes_nothing(self, admin_principal: SessionPrincipal) -> None:
        root = services.create_category(admin_principal, name="Cables")
        events = AuditEvent.objects.count()
        services.update_category(admin_principal, root.pk, name="Cables")
        assert AuditEvent.objects.count() == events


class TestMaterials:
    def test_create_audits_and_normalizes(
        self, admin_principal: SessionPrincipal, unit: UnitOfMeasure
    ) -> None:
        category = services.create_category(admin_principal, name="Cables")
        material = services.create_material(
            admin_principal,
            reference="  CAB-1 ",
            name=" Cable ",
            unit_id=unit.pk,
            category_id=category.pk,
            ean="",
            is_stock_item=True,
        )
        assert (material.reference, material.name, material.ean) == ("CAB-1", "Cable", None)
        assert material.is_active and material.is_stock_item
        event = AuditEvent.objects.get(action="material.created", object_id=material.pk)
        assert event.data["reference"] == "CAB-1"
        assert event.actor_label == "admin@example.org"

    @pytest.mark.parametrize(
        ("changes", "field"),
        [
            ({"reference": ""}, "reference"),
            ({"reference": "A\nB"}, "reference"),
            ({"name": "  "}, "name"),
            ({"ean": "123"}, "ean"),
            ({"ean": "12345678901234567"}, "ean"),
            ({"ean": "84X1234567890"}, "ean"),
        ],
    )
    def test_invariants(
        self,
        admin_principal: SessionPrincipal,
        unit: UnitOfMeasure,
        changes: dict[str, Any],
        field: str,
    ) -> None:
        values: dict[str, Any] = {"reference": "R-1", "name": "Thing", "unit_id": unit.pk} | changes
        with pytest.raises(ValidationError) as exc:
            services.create_material(admin_principal, **values)
        assert field in exc.value.error_dict

    def test_a_valid_ean_is_kept(
        self, admin_principal: SessionPrincipal, unit: UnitOfMeasure
    ) -> None:
        material = services.create_material(
            admin_principal, reference="R-1", name="Thing", unit_id=unit.pk, ean="8412345678905"
        )
        assert material.ean == "8412345678905"

    def test_the_reference_is_unique_ignoring_case(
        self, admin_principal: SessionPrincipal, unit: UnitOfMeasure, cable: Material
    ) -> None:
        with pytest.raises(ValidationError) as exc:
            services.create_material(
                admin_principal, reference="cab-2.5", name="Other", unit_id=unit.pk
            )
        assert "reference" in exc.value.error_dict

    def test_an_unknown_unit_or_category_is_refused(
        self, admin_principal: SessionPrincipal, unit: UnitOfMeasure, cable: Material
    ) -> None:
        missing = Material().pk  # a random id nothing uses
        with pytest.raises(ValidationError) as unit_exc:
            services.create_material(admin_principal, reference="R", name="N", unit_id=missing)
        assert "unit" in unit_exc.value.error_dict
        with pytest.raises(ValidationError) as category_exc:
            services.update_material(admin_principal, cable.pk, category_id=missing)
        assert "category" in category_exc.value.error_dict

    def test_update_changes_data_but_not_the_reference(
        self, admin_principal: SessionPrincipal, cable: Material
    ) -> None:
        updated = services.update_material(
            admin_principal, cable.pk, name="Cable 2,5", manufacturer=" Prysmian "
        )
        assert (updated.name, updated.manufacturer) == ("Cable 2,5", "Prysmian")
        event = AuditEvent.objects.get(action="material.updated", object_id=cable.pk)
        assert set(event.data) == {"name", "manufacturer"}
        with pytest.raises(ValueError, match="cannot be changed"):
            services.update_material(admin_principal, cable.pk, reference="OTHER")
        with pytest.raises(ValueError, match="cannot be changed"):
            services.update_material(admin_principal, cable.pk, is_active=False)

    def test_a_noop_update_writes_nothing(
        self, admin_principal: SessionPrincipal, cable: Material
    ) -> None:
        events = AuditEvent.objects.count()
        services.update_material(admin_principal, cable.pk, name=cable.name, ean=None)
        assert AuditEvent.objects.count() == events

    def test_deactivate_and_reactivate(
        self, admin_principal: SessionPrincipal, cable: Material
    ) -> None:
        assert not services.deactivate_material(admin_principal, cable.pk).is_active
        services.deactivate_material(admin_principal, cable.pk)  # already inactive: nothing
        assert AuditEvent.objects.filter(action="material.deactivated").count() == 1
        assert services.reactivate_material(admin_principal, cable.pk).is_active
        assert AuditEvent.objects.filter(action="material.reactivated").count() == 1

    def test_a_replayed_create_returns_the_first_result(
        self, admin_principal: SessionPrincipal, unit: UnitOfMeasure
    ) -> None:
        key = issue_key(admin_principal.user_id, "material.create")
        values: dict[str, Any] = {"reference": "R-1", "name": "Thing", "unit_id": unit.pk}
        first = services.create_material(admin_principal, idempotency_key=key, **values)
        again = services.create_material(admin_principal, idempotency_key=key, **values)
        assert again.pk == first.pk
        assert Material.objects.count() == 1
        with pytest.raises(IdempotencyConflict):
            services.create_material(
                admin_principal, idempotency_key=key, **(values | {"name": "Other"})
            )

    def test_the_unit_of_a_referenced_material_cannot_change(
        self, admin_principal: SessionPrincipal, cable: Material
    ) -> None:
        """C9: a composite foreign key onto ``(id, unit_id)`` holds the unit in place."""
        reference = AddCompositeForeignKey(
            name="scratch_material_unit_fk",
            table="scratch_line",
            columns=("material_id", "unit_id"),
            ref_table="catalog_material",
            ref_columns=("id", "unit_id"),
        )
        with connection.cursor() as cursor:
            cursor.execute(
                "CREATE TABLE scratch_line (id serial PRIMARY KEY, material_id uuid, unit_id uuid)"
            )
            for statement in reference.sql:
                assert isinstance(statement, str)
                cursor.execute(statement)
            cursor.execute(
                "INSERT INTO scratch_line (material_id, unit_id) VALUES (%s, %s)",
                [cable.pk, cable.unit_id],
            )
        other = UnitOfMeasure.objects.get(code="M")
        with pytest.raises(ValidationError) as exc:
            services.update_material(admin_principal, cable.pk, unit_id=other.pk)
        assert "unit" in exc.value.error_dict
        # Everything else still changes, and an unreferenced material may change its unit.
        services.update_material(admin_principal, cable.pk, name="Renamed")
        free = services.create_material(
            admin_principal, reference="FREE", name="Free", unit_id=other.pk
        )
        assert services.update_material(admin_principal, free.pk, unit_id=cable.unit_id)


class TestAuthorization:
    """``catalog.manage``: group administrators and material managers, nobody else."""

    @pytest.fixture
    def principals(self, org: Org, people: Any) -> dict[str, SessionPrincipal]:
        return {name: principal_for(getattr(people, name)) for name in people.all()}

    @pytest.mark.parametrize("who", ["group_admin", "mm_mad", "mm_sev"])
    def test_allowed(
        self, principals: dict[str, SessionPrincipal], unit: UnitOfMeasure, who: str
    ) -> None:
        material = services.create_material(
            principals[who], reference=f"R-{who}", name="Thing", unit_id=unit.pk
        )
        assert Material.objects.filter(pk=material.pk).exists()

    @pytest.mark.parametrize(
        "who",
        [
            "wo_mad",
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
    def test_denied(
        self,
        principals: dict[str, SessionPrincipal],
        people: Any,
        cable: Material,
        unit: UnitOfMeasure,
        who: str,
    ) -> None:
        principal = principals[who]
        events = AuditEvent.objects.count()
        for call in (
            lambda: services.create_material(principal, reference="X", name="X", unit_id=unit.pk),
            lambda: services.update_material(principal, cable.pk, name="Hijacked"),
            lambda: services.deactivate_material(principal, cable.pk),
            lambda: services.create_category(principal, name="X"),
        ):
            # 403 for who sees the catalog but may not change it; 404 for the object when
            # the catalog is not theirs to see at all (S7).
            with pytest.raises((AccessDenied, NotInScope)):
                call()
        assert AuditEvent.objects.count() == events
        assert Material.objects.get(pk=cable.pk).name != "Hijacked"

    def test_a_privileged_role_needs_session_mfa(self, people: Any, unit: UnitOfMeasure) -> None:
        without_mfa = principal_for(people.mm_mad, mfa_at=None)
        with pytest.raises(MFARequired):
            services.create_material(without_mfa, reference="X", name="X", unit_id=unit.pk)
        assert not Material.objects.exists()

    def test_a_deactivated_user_with_a_live_session_is_refused(
        self, make_user: Callable[..., User], org: Org, unit: UnitOfMeasure
    ) -> None:
        from tests.org import member

        user = member(Role.MATERIAL_MANAGER, email="late@example.org", branch=org.a_mad)
        principal = principal_for(user)
        User.objects.filter(pk=user.pk).update(is_active=False)
        with pytest.raises(AccessDenied):
            services.create_material(principal, reference="X", name="X", unit_id=unit.pk)

"""Deactivating a unit of measure (Q72 as amended, ADR-0030).

A unit is deactivated only once nothing alive uses it: no active material and no line of a
request or order in progress. Free-text request lines take only active units (a line keeps
the one it has), and a material comes back only after its unit.
"""

from dataclasses import dataclass
from decimal import Decimal
from typing import Any

import pytest
from django.core.exceptions import ValidationError
from django.forms import Form, ModelChoiceField
from django.urls import reverse
from django.utils import timezone, translation

from apps.access.context import AccessContext
from apps.access.exceptions import AccessDenied
from apps.access.principals import SessionPrincipal
from apps.audit.models import AuditEvent
from apps.catalog import selectors, services, unit_usage
from apps.catalog.models import Material, UnitOfMeasure
from apps.material_requests import ledger as request_ledger
from apps.material_requests import services as request_services
from apps.material_requests.forms import FreeLineForm, LineChangeForm
from apps.material_requests.models import MaterialRequest, MaterialRequestLine
from apps.purchasing import ledger as order_ledger
from apps.purchasing.models import PurchaseOrder
from tests import orders
from tests.org import Org, People, client_for
from tests.support import error_codes, principal_for

pytestmark = pytest.mark.django_db

HTMX = {"HX-Request": "true"}

Status = MaterialRequest.Status
Decision = MaterialRequestLine.Decision
OrderStatus = PurchaseOrder.Status


@pytest.fixture
def pallet(admin_principal: SessionPrincipal) -> UnitOfMeasure:
    return services.create_unit(admin_principal, code="PAL", name="Palet", decimals=1)


@pytest.fixture
def crate(admin_principal: SessionPrincipal) -> UnitOfMeasure:
    return services.create_unit(admin_principal, code="CAJ", name="Caja", decimals=0)


def deactivate(principal: SessionPrincipal, unit: UnitOfMeasure) -> UnitOfMeasure:
    return services.update_unit(principal, unit.pk, is_active=False)


def kinds(unit: UnitOfMeasure) -> list[tuple[str, int]]:
    return [(use.kind, use.count) for use in selectors.unit_uses(unit)]


def request_in(org: Org, people: People, status: str) -> MaterialRequest:
    """A request of the hospital project in ``status``, written directly (fixtures only)."""
    now = timezone.now()
    submitted = status not in {Status.DRAFT, Status.CANCELLED}
    closed = {"closed_at": now, "close_mode": "AUTO"} if status == Status.CLOSED else {}
    return MaterialRequest.objects.create(
        project=org.hospital,
        procurement_branch_id=org.hospital.procurement_branch_id,
        requested_by=people.tech_mad,
        status=status,
        number=f"SOL-U-{MaterialRequest.objects.count() + 1:05d}" if submitted else None,
        submitted_at=now if submitted else None,
        **closed,
    )


def decided(people: People, decision: str, approved: str | None) -> dict[str, Any]:
    if decision == Decision.PENDING:
        return {}
    qty = Decimal(approved) if decision == Decision.APPROVED and approved else None
    return {
        "decision": decision,
        "approved_qty": qty,
        "decided_by": people.mm_mad,
        "decided_at": timezone.now(),
    }


def closed_short(people: People) -> dict[str, Any]:
    return {
        "closed_short_at": timezone.now(),
        "closed_short_by": people.mm_mad,
        "closed_short_source": "LINE",
        "close_reason": "Not needed",
    }


def codes_offered(form: Form) -> set[str]:
    field = form.fields["unit"]
    assert isinstance(field, ModelChoiceField) and field.queryset is not None
    return {unit.code for unit in field.queryset}


def free_line(request: MaterialRequest, unit: UnitOfMeasure, **fields: Any) -> MaterialRequestLine:
    return MaterialRequestLine.objects.create(
        request=request,
        project=request.project,
        line_no=request.lines.count() + 1,
        free_text="Bandeja",
        unit=unit,
        requested_qty=Decimal("2.0"),
        **fields,
    )


# --- what blocks -----------------------------------------------------------------------------


class TestMaterials:
    def test_an_active_material_blocks_and_nothing_changes(
        self, admin_principal: SessionPrincipal, pallet: UnitOfMeasure
    ) -> None:
        services.create_material(admin_principal, reference="M-1", name="Thing", unit_id=pallet.pk)
        with pytest.raises(ValidationError) as exc:
            deactivate(admin_principal, pallet)
        assert error_codes(exc) == {"in_use"}
        assert UnitOfMeasure.objects.get(pk=pallet.pk).is_active
        assert not AuditEvent.objects.filter(action="unit.updated").exists()

    def test_once_its_materials_are_inactive_the_unit_goes_and_they_keep_it(
        self, admin_principal: SessionPrincipal, pallet: UnitOfMeasure
    ) -> None:
        made = services.create_material(
            admin_principal, reference="M-1", name="Thing", unit_id=pallet.pk
        )
        services.deactivate_material(admin_principal, made.pk)
        assert kinds(pallet) == []
        assert deactivate(admin_principal, pallet).is_active is False
        assert Material.objects.get(pk=made.pk).unit_id == pallet.pk

    def test_the_dialog_names_the_first_materials_by_reference(
        self, admin_principal: SessionPrincipal, pallet: UnitOfMeasure
    ) -> None:
        for number in range(7, 0, -1):
            services.create_material(
                admin_principal, reference=f"M-{number}", name="Thing", unit_id=pallet.pk
            )
        sleeping = services.create_material(
            admin_principal, reference="A-0", name="Old", unit_id=pallet.pk
        )
        services.deactivate_material(admin_principal, sleeping.pk)
        [use] = selectors.unit_uses(pallet)
        assert (use.kind, use.count, use.more) == ("materials", 7, True)
        assert use.examples == ("M-1", "M-2", "M-3", "M-4", "M-5")


@dataclass(frozen=True)
class LineCase:
    """A request line in a state: the request's status, the line's decision and approved
    quantity, whether it was closed short, and whether it blocks the deactivation."""

    status: str
    decision: str = Decision.PENDING
    approved: str | None = None
    short: bool = False
    blocks: bool = False


REQUEST_CASES = {
    "draft": LineCase(Status.DRAFT, blocks=True),
    "submitted": LineCase(Status.SUBMITTED, blocks=True),
    "submitted-approved-line": LineCase(Status.SUBMITTED, Decision.APPROVED, "2.0", blocks=True),
    "submitted-rejected-line": LineCase(Status.SUBMITTED, Decision.REJECTED),
    "approved": LineCase(Status.APPROVED, Decision.APPROVED, "2.0", blocks=True),
    "approved-zero": LineCase(Status.APPROVED, Decision.APPROVED, "0"),
    "approved-closed-short": LineCase(Status.APPROVED, Decision.APPROVED, "2.0", short=True),
    "approved-rejected-line": LineCase(Status.APPROVED, Decision.REJECTED),
    "rejected": LineCase(Status.REJECTED, Decision.REJECTED),
    "cancelled": LineCase(Status.CANCELLED),
    "closed": LineCase(Status.CLOSED, Decision.APPROVED, "2.0"),
}


class TestRequestLines:
    @pytest.mark.parametrize("case", REQUEST_CASES.values(), ids=REQUEST_CASES.keys())
    def test_a_line_in_progress_blocks(
        self,
        org: Org,
        people: People,
        admin_principal: SessionPrincipal,
        pallet: UnitOfMeasure,
        case: LineCase,
    ) -> None:
        extra = decided(people, case.decision, case.approved)
        extra |= closed_short(people) if case.short else {}
        free_line(request_in(org, people, case.status), pallet, **extra)
        assert kinds(pallet) == ([("request_lines", 1)] if case.blocks else [])
        if case.blocks:
            with pytest.raises(ValidationError) as exc:
                deactivate(admin_principal, pallet)
            assert error_codes(exc) == {"in_use"}
        else:
            assert deactivate(admin_principal, pallet).is_active is False


ORDER_CASES = [
    (OrderStatus.DRAFT, False, True),
    (OrderStatus.ISSUED, False, True),
    (OrderStatus.ISSUED, True, False),
    (OrderStatus.CLOSED, False, False),
    (OrderStatus.CANCELLED, False, False),
]


class TestOrderLines:
    @pytest.mark.parametrize(
        ("status", "short", "blocks"),
        ORDER_CASES,
        ids=["draft", "issued", "issued-closed-short", "closed", "cancelled"],
    )
    def test_a_line_in_progress_blocks(
        self,
        org: Org,
        people: People,
        admin_principal: SessionPrincipal,
        pallet: UnitOfMeasure,
        status: str,
        short: bool,
        blocks: bool,
    ) -> None:
        material = services.create_material(
            admin_principal, reference="M-1", name="Thing", unit_id=pallet.pk
        )
        services.deactivate_material(admin_principal, material.pk)  # the order line alone
        issuer = people.mm_mad if status != OrderStatus.DRAFT else None
        po = orders.order(org, status=status, issued_by=issuer)
        closure = {"closed_short_at": timezone.now(), "close_reason": "Short"} if short else {}
        orders.line(po, material, "3", **closure)
        assert kinds(pallet) == ([("order_lines", 1)] if blocks else [])
        if blocks:
            with pytest.raises(ValidationError):
                deactivate(admin_principal, pallet)
        else:
            assert deactivate(admin_principal, pallet).is_active is False


class TestRefusal:
    def test_the_refusal_says_everything_that_uses_the_unit(
        self, org: Org, people: People, admin_principal: SessionPrincipal, pallet: UnitOfMeasure
    ) -> None:
        material = services.create_material(
            admin_principal, reference="M-1", name="Thing", unit_id=pallet.pk
        )
        free_line(request_in(org, people, Status.DRAFT), pallet)
        orders.line(orders.order(org), material, "3")
        assert kinds(pallet) == [("materials", 1), ("request_lines", 1), ("order_lines", 1)]
        with translation.override("en"), pytest.raises(ValidationError) as exc:
            deactivate(admin_principal, pallet)
        with translation.override("en"):
            [message] = exc.value.messages
        for part in ("1 active material", "1 line of a request", "1 line of an order"):
            assert part in message

    def test_reactivating_needs_nothing(
        self, admin_principal: SessionPrincipal, pallet: UnitOfMeasure
    ) -> None:
        deactivate(admin_principal, pallet)
        assert services.update_unit(admin_principal, pallet.pk, is_active=True).is_active

    def test_a_change_of_name_is_not_a_deactivation(
        self, admin_principal: SessionPrincipal, pallet: UnitOfMeasure
    ) -> None:
        services.create_material(admin_principal, reference="M-1", name="Thing", unit_id=pallet.pk)
        assert services.update_unit(admin_principal, pallet.pk, name="Pallet").name == "Pallet"


class TestProviders:
    def test_both_document_apps_register_their_lines(self) -> None:
        registered = unit_usage._providers
        assert request_ledger.lines_using_unit in registered
        assert order_ledger.lines_using_unit in registered

    def test_a_provider_registers_once_and_unregisters(self, pallet: UnitOfMeasure) -> None:
        def fake(unit_id: Any) -> unit_usage.UnitUse:
            return unit_usage.UnitUse("fake", 2, "2 fakes")

        unregister = unit_usage.register_unit_users(fake)
        unit_usage.register_unit_users(fake)
        try:
            assert kinds(pallet) == [("fake", 2)]
            assert selectors.unit_uses(pallet)[0].more  # none of the two is named
        finally:
            unregister()
            unregister()  # a second call is harmless
        assert kinds(pallet) == []


# --- what an inactive unit may still do ------------------------------------------------------


@pytest.fixture
def asleep(admin_principal: SessionPrincipal, pallet: UnitOfMeasure) -> Material:
    """An inactive material whose unit is inactive too."""
    made = services.create_material(
        admin_principal, reference="M-1", name="Thing", unit_id=pallet.pk
    )
    services.deactivate_material(admin_principal, made.pk)
    deactivate(admin_principal, pallet)
    return made


def both_active(material: Material) -> tuple[bool, bool]:
    material = Material.objects.select_related("unit").get(pk=material.pk)
    return material.is_active, material.unit.is_active


class TestMaterialReactivation:
    def test_alone_it_is_refused_while_its_unit_is_inactive(
        self, admin_principal: SessionPrincipal, asleep: Material, pallet: UnitOfMeasure
    ) -> None:
        with translation.override("en"), pytest.raises(ValidationError) as exc:
            services.reactivate_material(admin_principal, asleep.pk)
        assert error_codes(exc) == {"inactive_unit"}
        with translation.override("en"):
            assert "PAL · Palet" in exc.value.messages[0]
        assert both_active(asleep) == (False, False)
        services.update_unit(admin_principal, pallet.pk, is_active=True)
        assert services.reactivate_material(admin_principal, asleep.pk).is_active

    def test_with_its_unit_both_come_back_in_one_step(
        self, admin_principal: SessionPrincipal, asleep: Material
    ) -> None:
        services.reactivate_material(admin_principal, asleep.pk, with_unit=True)
        assert both_active(asleep) == (True, True)
        # Every row of a test shares its transaction's time: the events are told by their data.
        back = AuditEvent.objects.filter(action="unit.updated", data__is_active__to=True)
        assert [event.data for event in back] == [{"is_active": {"from": False, "to": True}}]
        assert AuditEvent.objects.filter(action="material.reactivated").count() == 1

    def test_the_unit_needs_who_may_manage_units(self, people: People, asleep: Material) -> None:
        manager = principal_for(people.mm_mad)  # catalog.manage, not org.manage
        with pytest.raises(AccessDenied):
            services.reactivate_material(manager, asleep.pk, with_unit=True)
        assert both_active(asleep) == (False, False)

    def test_with_an_active_unit_nothing_more_is_needed(
        self, people: People, admin_principal: SessionPrincipal, pallet: UnitOfMeasure
    ) -> None:
        made = services.create_material(
            admin_principal, reference="M-2", name="Other", unit_id=pallet.pk
        )
        services.deactivate_material(admin_principal, made.pk)
        manager = principal_for(people.mm_mad)
        services.reactivate_material(manager, made.pk, with_unit=True)
        assert both_active(made) == (True, True)
        assert not AuditEvent.objects.filter(
            action="unit.updated", data__is_active__to=True
        ).exists()


class TestMaterialPage:
    def test_a_group_administrator_gets_the_note_and_one_button_for_both(
        self, people: People, asleep: Material
    ) -> None:
        page = client_for(people.group_admin).get(reverse("catalog:detail", args=[asleep.pk]))
        content = page.content.decode()
        assert 'data-testid="unit-inactive-note"' in content
        assert 'data-testid="reactivate-open"' in content
        assert 'data-testid="reactivate-form"' in content
        assert 'name="with_unit"' in content and 'value="True"' in content
        assert page.context["reactivate_dialog"] is not None

    def test_a_material_manager_is_told_who_can_and_gets_no_button_that_would_fail(
        self, people: People, asleep: Material
    ) -> None:
        page = client_for(people.mm_mad).get(reverse("catalog:detail", args=[asleep.pk]))
        content = page.content.decode()
        assert 'data-testid="unit-inactive-note"' in content
        assert page.context["reactivate_dialog"] is None
        assert reverse("catalog:reactivate", args=[asleep.pk]) not in content

    def test_with_an_active_unit_the_plain_button_stays(
        self, people: People, admin_principal: SessionPrincipal, pallet: UnitOfMeasure
    ) -> None:
        made = services.create_material(
            admin_principal, reference="M-2", name="Other", unit_id=pallet.pk
        )
        services.deactivate_material(admin_principal, made.pk)
        page = client_for(people.mm_mad).get(reverse("catalog:detail", args=[made.pk]))
        content = page.content.decode()
        assert 'data-testid="unit-inactive-note"' not in content
        assert reverse("catalog:reactivate", args=[made.pk]) in content
        assert page.context["reactivate_dialog"] is None

    def test_the_dialog_reactivates_both(self, people: People, asleep: Material) -> None:
        url = reverse("catalog:reactivate", args=[asleep.pk])
        response = client_for(people.group_admin).post(url, {"with_unit": "True"}, headers=HTMX)
        assert response.status_code == 204
        assert response["HX-Redirect"] == reverse("catalog:detail", args=[asleep.pk])
        assert both_active(asleep) == (True, True)

    def test_a_forged_request_without_the_right_is_refused(
        self, people: People, asleep: Material
    ) -> None:
        client = client_for(people.mm_mad)
        url = reverse("catalog:reactivate", args=[asleep.pk])
        assert client.post(url, {"with_unit": "True"}, headers=HTMX).status_code == 403
        plain = client.post(url)
        assert plain.status_code == 302  # the refusal is flashed on the material's page
        assert both_active(asleep) == (False, False)


class TestFreeTextLines:
    def test_a_new_line_takes_only_an_active_unit(
        self,
        org: Org,
        people: People,
        admin_principal: SessionPrincipal,
        pallet: UnitOfMeasure,
        crate: UnitOfMeasure,
    ) -> None:
        deactivate(admin_principal, pallet)
        draft = request_in(org, people, Status.DRAFT)
        requester = principal_for(people.tech_mad)
        with pytest.raises(ValidationError) as exc:
            request_services.add_line(
                requester,
                draft.pk,
                free_text="Bandeja",
                unit_id=pallet.pk,
                requested_qty=Decimal("1"),
            )
        assert error_codes(exc) == {"inactive"}
        line = request_services.add_line(
            requester, draft.pk, free_text="Bandeja", unit_id=crate.pk, requested_qty=Decimal("1")
        )
        assert line.unit_id == crate.pk

    def test_a_line_keeps_the_unit_it_has_but_takes_no_other_inactive_one(
        self,
        org: Org,
        people: People,
        pallet: UnitOfMeasure,
        crate: UnitOfMeasure,
    ) -> None:
        line = free_line(request_in(org, people, Status.DRAFT), pallet)
        # Deactivated since (a line written in the same instant as the deactivation, or a
        # closed request reopened): the line keeps its unit.
        UnitOfMeasure.objects.filter(pk__in=[pallet.pk, crate.pk]).update(is_active=False)
        requester = principal_for(people.tech_mad)
        changed = request_services.update_line(requester, line.pk, requested_qty=Decimal("3"))
        assert (changed.unit_id, changed.requested_qty) == (pallet.pk, Decimal("3"))
        with pytest.raises(ValidationError) as exc:
            request_services.update_line(requester, line.pk, unit_id=crate.pk)
        assert error_codes(exc) == {"inactive"}

    def test_the_forms_offer_active_units_and_the_lines_own(
        self,
        org: Org,
        people: People,
        admin_principal: SessionPrincipal,
        pallet: UnitOfMeasure,
        crate: UnitOfMeasure,
    ) -> None:
        deactivate(admin_principal, crate)
        ctx = AccessContext.for_user(people.mm_mad)
        offered = codes_offered(FreeLineForm(ctx=ctx))
        assert "PAL" in offered and "CAJ" not in offered
        assert "CAJ" not in codes_offered(LineChangeForm(ctx=ctx))
        line = free_line(request_in(org, people, Status.SUBMITTED), crate)
        assert {"PAL", "CAJ"} <= codes_offered(LineChangeForm(ctx=ctx, line=line))

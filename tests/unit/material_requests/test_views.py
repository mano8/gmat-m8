"""The requester's screens (M4.3): project, materials, review, submit, status, "My requests".

The flow is walked as the screens do, tap by tap: every POST a user makes counts as one
tap, and the done-when of the step is checked on the count (a 3-line request from recent
materials in at most six taps after the project, a double tap on submit makes one request).
"""

import re
from decimal import Decimal
from typing import Any
from uuid import UUID

import pytest
from django.core.exceptions import ValidationError
from django.test import Client
from django.urls import reverse

from apps.audit.models import AuditEvent
from apps.catalog.models import Material, UnitOfMeasure
from apps.core.locking import RetryExhausted
from apps.material_requests import ledger, services
from apps.material_requests.models import MaterialRequest, MaterialRequestLine
from apps.projects.models import Project
from tests.org import Org, People, Sentinel
from tests.org import client_for as signed_in
from tests.unit.material_requests.conftest import MakeRequest

pytestmark = pytest.mark.django_db

Status = MaterialRequest.Status
HTMX: dict[str, Any] = {"HTTP_HX_REQUEST": "true"}


def client_for(user: Any) -> Client:
    """Signed in, asking for English (the default language is Spanish)."""
    return signed_in(user, HTTP_ACCEPT_LANGUAGE="en")


def key_of(page: Any, name: str = "idempotency_key") -> str:
    match = re.search(rf'name="{name}"[^>]*value="([^"]+)"', page.content.decode())
    assert match is not None, name
    return match.group(1)


def start_data(page: Any, project: Project) -> dict[str, str]:
    """The form data of the button that opens ``project`` (its id and its own key)."""
    html = page.content.decode()
    form = html[: html.index(f'data-project="{project.code}"')].rsplit("<form", 1)[1]
    match = re.search(r'name="idempotency_key"[^>]*value="([^"]+)"', form)
    assert match is not None
    return {"project": str(project.pk), "idempotency_key": match.group(1)}


def start(client: Client, project: Project) -> Any:
    page = client.get(reverse("requests:new"))
    return client.post(reverse("requests:start"), start_data(page, project))


def request_id(response: Any) -> UUID:
    match = re.search(r"/requests/([0-9a-f-]{36})/", response["Location"])
    assert match is not None
    return UUID(match.group(1))


@pytest.fixture
def ud() -> UnitOfMeasure:
    return UnitOfMeasure.objects.get(code="UD")


@pytest.fixture
def materials(cable: Material, breaker: Material, ud: UnitOfMeasure) -> dict[str, Material]:
    return {
        "cable": cable,
        "diff": breaker,
        "box": Material.objects.create(reference="BOX-01", name="Caja registro", unit=ud),
        "old": Material.objects.create(reference="OLD-01", name="Obsoleto", unit=ud),
    }


def previous_request(
    make_request: MakeRequest, user: Any, materials: dict[str, Material], *names: str
) -> MaterialRequest:
    """A request the user made before, with one line per named material."""
    previous = make_request(Status.SUBMITTED, user=user, lines=0)
    for number, name in enumerate(names, start=1):
        material = materials[name]
        MaterialRequestLine.objects.create(
            request=previous,
            project=previous.project,
            line_no=number,
            material=material,
            unit=material.unit,
            requested_qty=Decimal("1"),
        )
    return previous


# --- the flow -----------------------------------------------------------------------------


class TestTheFlow:
    def test_three_recent_materials_take_five_taps_after_the_project(
        self, org: Org, people: People, make_request: MakeRequest, materials: dict[str, Material]
    ) -> None:
        previous_request(make_request, people.tech_mad, materials, "cable", "diff", "box")
        client, taps = client_for(people.tech_mad), []

        opened = start(client, org.hospital)  # choosing the project is not counted
        assert opened.status_code == 302
        detail_url = opened["Location"]
        page = client.get(detail_url)
        draft_id = request_id(opened)
        assert page.content.decode().count('data-material="') == 3  # the recents are on show

        for name in ("cable", "diff", "box"):
            taps.append("add " + name)
            added = client.post(
                reverse("requests:add_line", args=[draft_id]),
                {"material": materials[name].pk},
                **HTMX,
            )
            assert added.status_code == 200
        taps.append("review")
        review = client.get(reverse("requests:review", args=[draft_id]))
        assert review.status_code == 200
        taps.append("submit")
        done = client.post(
            reverse("requests:submit", args=[draft_id]),
            {"priority": "NORMAL", "idempotency_key": key_of(review)},
        )
        assert done.status_code == 302 and done["Location"] == detail_url

        assert len(taps) <= 6
        submitted = MaterialRequest.objects.get(pk=draft_id)
        assert submitted.status == Status.SUBMITTED and submitted.number
        assert submitted.lines.count() == 3
        status_page = client.get(detail_url).content.decode()
        assert submitted.number in status_page
        assert (
            "Withdraw" in status_page
            or reverse("requests:withdraw", args=[draft_id]) in status_page
        )

    def test_a_double_tap_on_submit_makes_one_request(
        self, org: Org, people: People, make_request: MakeRequest
    ) -> None:
        draft = make_request()
        client = client_for(people.tech_mad)
        review = client.get(reverse("requests:review", args=[draft.pk]))
        data = {"priority": "HIGH", "needed_by": "", "notes": "", "idempotency_key": key_of(review)}
        url = reverse("requests:submit", args=[draft.pk])
        first, second = client.post(url, data), client.post(url, data)
        assert first.status_code == second.status_code == 302
        assert first["Location"] == second["Location"]
        assert MaterialRequest.objects.count() == 1
        assert AuditEvent.objects.filter(action="request.submitted").count() == 1
        draft.refresh_from_db()
        assert (draft.status, draft.priority) == (Status.SUBMITTED, "HIGH")

    def test_a_double_tap_on_a_project_makes_one_draft(self, org: Org, people: People) -> None:
        client = client_for(people.tech_mad)
        page = client.get(reverse("requests:new"))
        data = start_data(page, org.hospital)
        first = client.post(reverse("requests:start"), data)
        second = client.post(reverse("requests:start"), data)
        assert first["Location"] == second["Location"]
        assert MaterialRequest.objects.count() == 1
        # Another project is another command, with its own key.
        other = start(client, org.sev_for_mad)
        assert request_id(other) != request_id(first)
        assert MaterialRequest.objects.count() == 2

    def test_the_review_step_is_saved(
        self, org: Org, people: People, make_request: MakeRequest
    ) -> None:
        draft, client = make_request(), client_for(people.tech_mad)
        review = client.get(reverse("requests:review", args=[draft.pk]))
        client.post(
            reverse("requests:submit", args=[draft.pk]),
            {
                "priority": "URGENT",
                "needed_by": "2099-03-04",
                "notes": "Plant 2",
                "idempotency_key": key_of(review),
            },
        )
        draft.refresh_from_db()
        assert (str(draft.needed_by), draft.priority, draft.notes) == (
            "2099-03-04",
            "URGENT",
            "Plant 2",
        )

    def test_the_review_refuses_a_past_date_and_an_empty_request(
        self, people: People, make_request: MakeRequest
    ) -> None:
        client = client_for(people.tech_mad)
        draft = make_request()
        key = key_of(client.get(reverse("requests:review", args=[draft.pk])))
        past = client.post(
            reverse("requests:submit", args=[draft.pk]),
            {"priority": "NORMAL", "needed_by": "2001-01-01", "idempotency_key": key},
        )
        assert past.status_code == 200 and "cannot be in the past" in past.content.decode()
        empty = make_request(lines=0)
        key = key_of(client.get(reverse("requests:review", args=[empty.pk])))
        refused = client.post(
            reverse("requests:submit", args=[empty.pk]),
            {"priority": "NORMAL", "idempotency_key": key},
        )
        assert refused.status_code == 200
        assert "Add at least one line" in refused.content.decode()
        empty.refresh_from_db()
        assert empty.status == Status.DRAFT

    def test_a_forged_form_key_is_refused(self, people: People, make_request: MakeRequest) -> None:
        draft = make_request()
        response = client_for(people.tech_mad).post(
            reverse("requests:submit", args=[draft.pk]),
            {"priority": "NORMAL", "idempotency_key": "forged"},
        )
        assert response.status_code == 400

    def test_discarding_a_draft(self, people: People, make_request: MakeRequest) -> None:
        draft, client = make_request(), client_for(people.tech_mad)
        response = client.post(reverse("requests:cancel", args=[draft.pk]), {"reason": "typo"})
        assert response.status_code == 302 and response["Location"] == reverse("requests:list")
        draft.refresh_from_db()
        assert draft.status == Status.CANCELLED
        assert AuditEvent.objects.filter(action="request.discarded").exists()
        # A discarded draft is no longer the requester's to change.
        assert client.post(reverse("requests:cancel", args=[draft.pk])).status_code == 403


# --- projects ---------------------------------------------------------------------------


class TestProjects:
    def test_only_active_projects_the_user_may_request_for(self, org: Org, people: People) -> None:
        page = client_for(people.tech_mad).get(reverse("requests:new")).content.decode()
        assert "HOSP-X" in page and "SEV-2" in page
        assert "MAD-1" not in page  # on hold
        assert "SEV-1" not in page  # not in the technician's reach
        partner = client_for(people.partner).get(reverse("requests:new")).content.decode()
        assert "HOSP-X" in partner and "SEV-2" not in partner

    def test_the_latest_used_project_comes_first_and_search_narrows(
        self, org: Org, people: People, make_request: MakeRequest
    ) -> None:
        client = client_for(people.tech_mad)
        before = client.get(reverse("requests:new")).content.decode()
        assert before.index("HOSP-X") < before.index("SEV-2")  # code order: no history yet
        make_request(project=org.sev_for_mad)
        after = client.get(reverse("requests:new")).content.decode()
        assert after.index("SEV-2") < after.index("HOSP-X")
        found = client.get(reverse("requests:new"), {"q": "hosp"}, **HTMX).content.decode()
        assert "HOSP-X" in found and "SEV-2" not in found and "<html" not in found

    def test_a_project_off_the_list_cannot_be_started(self, org: Org, people: People) -> None:
        client = client_for(people.tech_mad)
        page = client.get(reverse("requests:new"))
        key = start_data(page, org.hospital)["idempotency_key"]
        for project in (org.sev_works, org.mad_hold):
            response = client.post(
                reverse("requests:start"), {"project": project.pk, "idempotency_key": key}
            )
            assert response.status_code == 302 and response["Location"] == reverse("requests:new")
        assert not MaterialRequest.objects.exists()

    def test_a_project_put_on_hold_meanwhile_is_refused_by_the_service(
        self, org: Org, people: People
    ) -> None:
        client = client_for(people.tech_mad)
        page = client.get(reverse("requests:new"))
        data = start_data(page, org.hospital)
        Project.objects.filter(pk=org.hospital.pk).update(status=Project.Status.ON_HOLD)
        response = client.post(reverse("requests:start"), data)
        assert response.status_code == 302 and response["Location"] == reverse("requests:new")
        assert not MaterialRequest.objects.exists()


# --- materials ----------------------------------------------------------------------------


class TestMaterials:
    def test_recent_materials_first_active_only_and_search(
        self, org: Org, people: People, make_request: MakeRequest, materials: dict[str, Material]
    ) -> None:
        previous_request(make_request, people.tech_mad, materials, "box", "old", "diff")
        previous_request(make_request, people.partner, materials, "cable")  # someone else's
        Material.objects.filter(pk=materials["old"].pk).update(is_active=False)
        draft, client = make_request(lines=0), client_for(people.tech_mad)
        url = reverse("requests:materials", args=[draft.pk])
        recent = client.get(url).content.decode()
        assert "Recent materials" in recent
        assert recent.index("DIF-40") < recent.index("BOX-01")  # newest line first
        assert "OLD-01" not in recent and "CAB-25" not in recent
        found = client.get(url, {"q": "cable"}).content.decode()
        assert "CAB-25" in found and "Recent materials" not in found
        assert "No material matches." in client.get(url, {"q": "zzzz"}).content.decode()

    def test_the_picker_is_for_the_owner_of_a_draft(
        self, people: People, make_request: MakeRequest
    ) -> None:
        draft = make_request()
        url = reverse("requests:materials", args=[draft.pk])
        assert client_for(people.mm_mad).get(url).status_code == 403  # sees it, not theirs
        assert client_for(people.partner).get(url).status_code == 404  # does not see it

    def test_adding_the_same_material_adds_up(
        self, people: People, make_request: MakeRequest, materials: dict[str, Material]
    ) -> None:
        draft, client = make_request(lines=0), client_for(people.tech_mad)
        url = reverse("requests:add_line", args=[draft.pk])
        for _ in range(2):
            client.post(url, {"material": materials["diff"].pk})
        client.post(url, {"material": materials["cable"].pk, "requested_qty": "2.5"})
        lines = {line.description: line.requested_qty for line in draft.lines.all()}
        assert lines == {
            str(materials["diff"]): Decimal("2"),
            str(materials["cable"]): Decimal("2.5"),
        }

    def test_without_javascript_the_answer_is_a_redirect(
        self, people: People, make_request: MakeRequest, materials: dict[str, Material]
    ) -> None:
        draft = make_request(lines=0)
        response = client_for(people.tech_mad).post(
            reverse("requests:add_line", args=[draft.pk]), {"material": materials["box"].pk}
        )
        assert response.status_code == 302
        assert response["Location"] == reverse("requests:detail", args=[draft.pk])

    def test_an_inactive_or_unknown_material_is_refused(
        self, people: People, make_request: MakeRequest, materials: dict[str, Material]
    ) -> None:
        draft, client = make_request(lines=0), client_for(people.tech_mad)
        Material.objects.filter(pk=materials["old"].pk).update(is_active=False)
        url = reverse("requests:add_line", args=[draft.pk])
        for value in (materials["old"].pk, "not-an-id"):
            response = client.post(url, {"material": value}, **HTMX)
            assert response.status_code == 200
            assert 'data-testid="lines-error"' in response.content.decode()
        assert not draft.lines.exists()

    def test_a_precision_the_unit_does_not_allow_is_explained(
        self, people: People, make_request: MakeRequest, materials: dict[str, Material]
    ) -> None:
        draft = make_request(lines=0)
        response = client_for(people.tech_mad).post(
            reverse("requests:add_line", args=[draft.pk]),
            {"material": materials["box"].pk, "requested_qty": "1.5"},
            **HTMX,
        )
        assert "at most 0 decimals" in response.content.decode()
        assert not draft.lines.exists()

    def test_a_material_outside_the_catalog(
        self, people: People, make_request: MakeRequest, ud: UnitOfMeasure
    ) -> None:
        draft, client = make_request(lines=0), client_for(people.tech_mad)
        page = client.get(reverse("requests:detail", args=[draft.pk]))
        data = {
            "free_text": "Soporte especial",
            "unit": ud.pk,
            "requested_qty": "4",
            "idempotency_key": key_of(page),
        }
        url = reverse("requests:add_free_line", args=[draft.pk])
        added = client.post(url, data, **HTMX)
        assert added.status_code == 200
        assert client.post(url, data, **HTMX).status_code == 200  # a double tap: one line
        line = draft.lines.get()
        assert (line.free_text, line.material_id, line.requested_qty) == (
            "Soporte especial",
            None,
            Decimal("4"),
        )
        # The answer carries a fresh key for the next line, so adding another works.
        fresh = key_of(added)
        assert fresh != data["idempotency_key"]
        second = client.post(
            url, {**data, "free_text": "Tornillos", "idempotency_key": fresh}, **HTMX
        )
        assert draft.lines.count() == 2
        invalid = client.post(url, {**data, "free_text": "", "idempotency_key": "x"}, **HTMX)
        assert 'data-testid="lines-error"' in invalid.content.decode()
        assert "<details" in invalid.content.decode() and " open" in invalid.content.decode()
        precision = client.post(
            url,
            {
                **data,
                "free_text": "Otro",
                "requested_qty": "1.5",
                "idempotency_key": key_of(second),
            },
            **HTMX,
        )
        assert "at most 0 decimals" in precision.content.decode()
        assert draft.lines.count() == 2

    def test_a_forged_key_on_a_free_line_is_refused(
        self, people: People, make_request: MakeRequest, ud: UnitOfMeasure
    ) -> None:
        draft = make_request(lines=0)
        response = client_for(people.tech_mad).post(
            reverse("requests:add_free_line", args=[draft.pk]),
            {"free_text": "x", "unit": ud.pk, "requested_qty": "1", "idempotency_key": "forged"},
        )
        assert response.status_code == 400


class TestTheStepper:
    @pytest.fixture
    def line(self, make_request: MakeRequest) -> MaterialRequestLine:
        return make_request().lines.get()  # 10 metres of cable

    def post(self, people: People, line: MaterialRequestLine, **data: str) -> Any:
        return client_for(people.tech_mad).post(
            reverse("requests:update_line", args=[line.pk]), data, **HTMX
        )

    def test_plus_and_minus_move_by_one_and_the_field_sets_it(
        self, people: People, line: MaterialRequestLine
    ) -> None:
        self.post(people, line, requested_qty="10", step="inc")
        line.refresh_from_db()
        assert line.requested_qty == Decimal("11")
        self.post(people, line, requested_qty="11", step="dec")
        self.post(people, line, requested_qty="10", step="dec")
        line.refresh_from_db()
        assert line.requested_qty == Decimal("9")
        self.post(people, line, requested_qty="2.25")
        line.refresh_from_db()
        assert line.requested_qty == Decimal("2.25")

    def test_the_quantity_cannot_reach_zero_or_be_invalid(
        self, people: People, line: MaterialRequestLine
    ) -> None:
        response = self.post(people, line, requested_qty="1", step="dec")
        assert "remove the line instead" in response.content.decode()
        for bad in ("", "abc", "0", "-3", "1.2345", "2.2555"):
            assert (
                'data-testid="lines-error"'
                in self.post(people, line, requested_qty=bad).content.decode()
            ), bad
        line.refresh_from_db()
        assert line.requested_qty == Decimal("10")

    def test_remove_a_line(self, people: People, make_request: MakeRequest) -> None:
        draft = make_request(lines=2)
        first = draft.lines.get(line_no=1)
        client = client_for(people.tech_mad)
        response = client.post(reverse("requests:remove_line", args=[first.pk]), **HTMX)
        assert response.status_code == 200
        assert list(draft.lines.values_list("line_no", flat=True)) == [2]
        assert client.post(reverse("requests:remove_line", args=[first.pk])).status_code == 404

    def test_a_line_of_a_submitted_request_is_not_changed(
        self, people: People, make_request: MakeRequest
    ) -> None:
        line = make_request(Status.SUBMITTED).lines.get()
        client = client_for(people.tech_mad)
        update = client.post(
            reverse("requests:update_line", args=[line.pk]), {"requested_qty": "3"}, **HTMX
        )
        assert "Lines change only while the request is a draft" in update.content.decode()
        remove = client.post(reverse("requests:remove_line", args=[line.pk]), **HTMX)
        assert "Lines change only while the request is a draft" in remove.content.decode()
        line.refresh_from_db()
        assert line.requested_qty == Decimal("10")

    def test_without_javascript_an_error_is_flashed(
        self, people: People, line: MaterialRequestLine
    ) -> None:
        client = client_for(people.tech_mad)
        response = client.post(
            reverse("requests:update_line", args=[line.pk]), {"requested_qty": "0"}
        )
        assert response.status_code == 302
        follow = client.get(response["Location"]).content.decode()
        assert "must be positive" in follow


# --- the status page and withdrawing -----------------------------------------------------


class TestStatusPage:
    def test_a_submitted_request_offers_withdraw_until_a_line_is_decided(
        self, people: People, make_request: MakeRequest
    ) -> None:
        submitted, client = make_request(Status.SUBMITTED), client_for(people.tech_mad)
        url = reverse("requests:withdraw", args=[submitted.pk])
        page = client.get(reverse("requests:detail", args=[submitted.pk])).content.decode()
        assert url in page and str(submitted.number) in page
        MaterialRequestLine.objects.filter(request=submitted).update(
            decision="REJECTED", decided_by=people.mm_mad, decided_at=submitted.submitted_at
        )
        page = client.get(reverse("requests:detail", args=[submitted.pk])).content.decode()
        assert url not in page

    def test_withdraw_returns_the_request_to_draft(
        self, people: People, make_request: MakeRequest
    ) -> None:
        submitted, client = make_request(Status.SUBMITTED), client_for(people.tech_mad)
        response = client.post(reverse("requests:withdraw", args=[submitted.pk]))
        assert response.status_code == 302
        submitted.refresh_from_db()
        assert submitted.status == Status.DRAFT
        page = client.get(response["Location"]).content.decode()
        assert "Add materials" in page and "Review and submit" in page

    def test_withdraw_after_a_decision_is_refused(
        self, people: People, make_request: MakeRequest
    ) -> None:
        submitted = make_request(Status.SUBMITTED, decided=True)
        client = client_for(people.tech_mad)
        response = client.post(reverse("requests:withdraw", args=[submitted.pk]))
        follow = client.get(response["Location"]).content.decode()
        assert "can no longer be withdrawn" in follow
        submitted.refresh_from_db()
        assert submitted.status == Status.SUBMITTED

    def test_delivered_and_outstanding_per_line(
        self,
        people: People,
        make_request: MakeRequest,
        flows_of: dict[UUID, ledger.Flows],
    ) -> None:
        approved = make_request(Status.APPROVED, decided=True, lines=2)
        first, second = approved.lines.order_by("line_no")
        flows_of[first.pk] = ledger.Flows(delivered=Decimal("2"))
        page = client_for(people.tech_mad).get(reverse("requests:detail", args=[approved.pk]))
        html = page.content.decode()
        row = html[html.index('data-line="1"') : html.index('data-line="2"')]
        assert "2 M" in row and "3 M" in row  # delivered 2, outstanding 5 minus 2
        rest = html[html.index('data-line="2"') :]
        assert "5 M" in rest  # nothing delivered yet: outstanding is the approved quantity
        assert second.pk != first.pk

    def test_a_decision_note_and_a_closure_are_shown(
        self, people: People, make_request: MakeRequest
    ) -> None:
        request = make_request(Status.APPROVED, decided=True, lines=2)
        one, two = request.lines.order_by("line_no")
        MaterialRequestLine.objects.filter(pk=one.pk).update(
            closed_short_at=request.submitted_at,
            closed_short_by=people.mm_mad,
            closed_short_source="LINE",
            close_reason="Supplier discontinued it",
        )
        MaterialRequestLine.objects.filter(pk=two.pk).update(
            decision="REJECTED", approved_qty=None, decision_note="Use the stock one"
        )
        html = (
            client_for(people.tech_mad)
            .get(reverse("requests:detail", args=[request.pk]))
            .content.decode()
        )
        assert "Closed short" in html and "Supplier discontinued it" in html
        assert "Rejected" in html and "Use the stock one" in html

    def test_a_reviewer_reads_it_without_the_requester_controls(
        self, people: People, make_request: MakeRequest
    ) -> None:
        draft = make_request()
        page = client_for(people.mm_mad).get(reverse("requests:detail", args=[draft.pk]))
        html = page.content.decode()
        assert page.status_code == 200
        assert reverse("requests:add_line", args=[draft.pk]) not in html
        assert reverse("requests:cancel", args=[draft.pk]) not in html
        submitted = make_request(Status.SUBMITTED)
        html = (
            client_for(people.mm_mad)
            .get(reverse("requests:detail", args=[submitted.pk]))
            .content.decode()
        )
        assert reverse("requests:withdraw", args=[submitted.pk]) not in html

    def test_someone_elses_request_is_not_found(
        self, people: People, make_request: MakeRequest
    ) -> None:
        theirs = make_request(user=people.partner)
        assert (
            client_for(people.tech_mad)
            .get(reverse("requests:detail", args=[theirs.pk]))
            .status_code
            == 404
        )
        assert (
            client_for(people.tech_mad)
            .post(reverse("requests:withdraw", args=[theirs.pk]))
            .status_code
            == 404
        )


# --- My requests ------------------------------------------------------------------------


class TestMyRequests:
    def test_lists_only_own_requests_with_status_chips_and_filters(
        self, org: Org, people: People, make_request: MakeRequest
    ) -> None:
        draft = make_request()
        approved = make_request(Status.APPROVED, decided=True)
        make_request(Status.SUBMITTED, user=people.partner)  # not theirs
        client = client_for(people.tech_mad)
        page = client.get(reverse("requests:list")).content.decode()
        assert str(draft.pk) in page and str(approved.pk) in page
        assert str(approved.number) in page and page.count("data-request=") == 2
        only = client.get(reverse("requests:list"), {"status": "APPROVED"}).content.decode()
        assert str(approved.pk) in only and str(draft.pk) not in only
        ignored = client.get(reverse("requests:list"), {"status": "NOPE"}).content.decode()
        assert page.count("data-request=") == ignored.count("data-request=")

    def test_per_line_delivered_and_outstanding_and_pages(
        self, people: People, make_request: MakeRequest, flows_of: dict[UUID, ledger.Flows]
    ) -> None:
        approved = make_request(Status.APPROVED, decided=True)
        flows_of[approved.lines.get().pk] = ledger.Flows(delivered=Decimal("4"))
        for _ in range(11):
            make_request()
        client = client_for(people.tech_mad)
        first = client.get(reverse("requests:list")).content.decode()
        second = client.get(reverse("requests:list"), {"page": 2}).content.decode()
        assert first.count("data-request=") == 10 and second.count("data-request=") == 2
        assert "4 M" in second and "1 M" in second  # delivered 4, outstanding 5 minus 4

    def test_an_empty_list_offers_the_first_request(self, people: People) -> None:
        page = client_for(people.tech_mad).get(reverse("requests:list")).content.decode()
        assert reverse("requests:new") in page and "No requests" in page
        reviewer = client_for(people.wo_mad).get(reverse("requests:list")).content.decode()
        assert reverse("requests:new") not in reviewer and "not made any request" in reviewer

    def test_home_and_navigation_lead_to_the_flow(self, people: People) -> None:
        home = client_for(people.tech_mad).get(reverse("core:home")).content.decode()
        assert reverse("requests:new") in home and reverse("requests:list") in home
        warehouse = client_for(people.wo_mad).get(reverse("core:home")).content.decode()
        assert reverse("requests:new") not in warehouse
        assert reverse("requests:list") in warehouse
        nobody = client_for(people.nobody).get(reverse("core:home")).content.decode()
        assert reverse("requests:list") not in nobody


class TestPartners:
    def test_a_partner_runs_the_same_flow_and_sees_no_prices_or_suppliers(
        self, org: Org, people: People, materials: dict[str, Material]
    ) -> None:
        client = client_for(people.partner)
        opened = start(client, org.hospital)
        draft_id = request_id(opened)
        pages = [client.get(opened["Location"])]
        client.post(
            reverse("requests:add_line", args=[draft_id]), {"material": materials["diff"].pk}
        )
        review = client.get(reverse("requests:review", args=[draft_id]))
        pages += [review, client.get(reverse("requests:list"))]
        done = client.post(
            reverse("requests:submit", args=[draft_id]),
            {"priority": "NORMAL", "idempotency_key": key_of(review)},
        )
        assert done.status_code == 302
        pages += [client.get(done["Location"]), client.get(reverse("requests:list"))]
        for page in pages:
            html = page.content.decode()
            assert page.status_code == 200
            assert not [s for s in Sentinel if s.found_in(html)]
            assert "price" not in html.lower()
        assert MaterialRequest.objects.get(pk=draft_id).requested_by_id == people.partner.pk


class TestAccess:
    def test_a_reviewer_cannot_change_someone_elses_draft(
        self, people: People, make_request: MakeRequest, materials: dict[str, Material]
    ) -> None:
        draft = make_request()
        client = client_for(people.mm_mad)
        for name, args, data in (
            ("add_line", [draft.pk], {"material": materials["box"].pk}),
            ("submit", [draft.pk], {"priority": "NORMAL"}),
            ("cancel", [draft.pk], {}),
            ("update_line", [draft.lines.get().pk], {"requested_qty": "3"}),
            ("remove_line", [draft.lines.get().pk], {}),
        ):
            assert client.post(reverse(f"requests:{name}", args=args), data).status_code == 403, (
                name
            )
        assert client.get(reverse("requests:review", args=[draft.pk])).status_code == 403
        draft.refresh_from_db()
        assert draft.status == Status.DRAFT and draft.lines.get().requested_qty == Decimal("10")

    def test_a_draft_of_a_project_put_on_hold_still_opens(
        self, org: Org, people: People, make_request: MakeRequest
    ) -> None:
        draft = make_request()
        Project.objects.filter(pk=org.hospital.pk).update(status=Project.Status.ON_HOLD)
        # Own requests stay visible when the project goes on hold; submitting is refused.
        client = client_for(people.tech_mad)
        assert client.get(reverse("requests:detail", args=[draft.pk])).status_code == 200
        review = client.get(reverse("requests:review", args=[draft.pk]))
        refused = client.post(
            reverse("requests:submit", args=[draft.pk]),
            {"priority": "NORMAL", "idempotency_key": key_of(review)},
        )
        assert refused.status_code == 200
        assert "accepts no new requests" in refused.content.decode()


class TestLocalizedQuantities:
    def test_a_spanish_user_types_the_decimal_comma(
        self, people: People, make_request: MakeRequest, materials: dict[str, Material]
    ) -> None:
        draft = make_request(lines=0)
        client = signed_in(people.tech_mad)  # Spanish is the default language
        client.post(
            reverse("requests:add_line", args=[draft.pk]),
            {"material": materials["cable"].pk, "requested_qty": "2,5"},
        )
        line = draft.lines.get()
        assert line.requested_qty == Decimal("2.5")
        page = client.get(reverse("requests:detail", args=[draft.pk])).content.decode()
        assert 'value="2,5"' in page


class TestServiceFailures:
    """What a view answers when its service gives up or refuses after the form was valid."""

    @pytest.mark.parametrize(
        ("service", "url_name", "target", "data"),
        [
            ("create_draft", "start", None, {}),
            ("add_line", "add_line", "draft", {"material": "{material}"}),
            (
                "add_line",
                "add_free_line",
                "draft",
                {"free_text": "x", "unit": "{unit}", "requested_qty": "1"},
            ),
            ("update_line", "update_line", "line", {"requested_qty": "3"}),
            ("remove_line", "remove_line", "line", {}),
            ("submit", "submit", "draft", {"priority": "NORMAL"}),
            ("withdraw", "withdraw", "submitted", {}),
            ("cancel", "cancel", "draft", {}),
        ],
    )
    def test_a_service_that_keeps_conflicting_answers_409(
        self,
        monkeypatch: pytest.MonkeyPatch,
        org: Org,
        people: People,
        make_request: MakeRequest,
        materials: dict[str, Material],
        ud: UnitOfMeasure,
        service: str,
        url_name: str,
        target: str | None,
        data: dict[str, str],
    ) -> None:
        def give_up(*args: Any, **kwargs: Any) -> None:
            raise RetryExhausted("deadlocks")

        draft, submitted = make_request(), make_request(Status.SUBMITTED)
        client = client_for(people.tech_mad)
        if target is None:
            page = client.get(reverse("requests:new"))
            args, data = [], start_data(page, org.hospital)
        else:
            args = [
                {"draft": draft.pk, "submitted": submitted.pk, "line": draft.lines.get().pk}[target]
            ]
            key = key_of(client.get(reverse("requests:review", args=[draft.pk])))
            fill = {"material": materials["box"].pk, "unit": ud.pk}
            data = {k: v.format(**fill) for k, v in data.items()} | {"idempotency_key": key}
        monkeypatch.setattr(services, service, give_up)
        response = client.post(reverse(f"requests:{url_name}", args=args), data)
        assert response.status_code == 409

    def test_a_start_refused_by_the_service_goes_back_to_the_list(
        self, monkeypatch: pytest.MonkeyPatch, org: Org, people: People
    ) -> None:
        def refuse(*args: Any, **kwargs: Any) -> None:
            raise ValidationError("The project's branch is inactive.")

        client = client_for(people.tech_mad)
        page = client.get(reverse("requests:new"))
        data = start_data(page, org.hospital)
        monkeypatch.setattr(services, "create_draft", refuse)
        response = client.post(reverse("requests:start"), data)
        assert response["Location"] == reverse("requests:new")
        assert "branch is inactive" in client.get(response["Location"]).content.decode()

    def test_a_key_the_service_refuses_is_a_400_or_409(
        self, monkeypatch: pytest.MonkeyPatch, org: Org, people: People
    ) -> None:
        client = client_for(people.tech_mad)
        response = client.post(
            reverse("requests:start"), {"project": org.hospital.pk, "idempotency_key": "forged"}
        )
        assert response.status_code == 400

    def test_a_cancel_refused_by_the_service_is_explained(
        self, monkeypatch: pytest.MonkeyPatch, people: People, make_request: MakeRequest
    ) -> None:
        def refuse(*args: Any, **kwargs: Any) -> None:
            raise ValidationError("Nothing can be cancelled now.")

        draft, client = make_request(), client_for(people.tech_mad)
        monkeypatch.setattr(services, "cancel", refuse)
        response = client.post(reverse("requests:cancel", args=[draft.pk]))
        assert response["Location"] == reverse("requests:detail", args=[draft.pk])
        assert "Nothing can be cancelled now." in client.get(response["Location"]).content.decode()

"""The reviewer's screens (M4.4): inbox, review page, material manager's tools, history.

Each action is a POST-only URL that calls its service and redirects back to the review page
with a flash message; the page is read like a user does, and the data-testid selectors the
e2e layer will use are checked present.
"""

from decimal import Decimal
from typing import Any

import pytest
from django.test import Client
from django.urls import reverse

from apps.access.context import AccessContext
from apps.audit.models import AuditEvent
from apps.catalog.models import Material, UnitOfMeasure
from apps.core.locking import RetryExhausted
from apps.material_requests import ledger, selectors, services
from apps.material_requests.models import MaterialRequest, MaterialRequestLine
from tests.org import Org, People
from tests.org import client_for as signed_in
from tests.support import principal_for
from tests.unit.material_requests.conftest import MakeRequest

pytestmark = pytest.mark.django_db

Status = MaterialRequest.Status
Decision = MaterialRequestLine.Decision


def client_for(user: Any) -> Client:
    return signed_in(user, HTTP_ACCEPT_LANGUAGE="en")


def page(client: Client, request: MaterialRequest) -> str:
    response = client.get(reverse("requests:examine", args=[request.pk]))
    assert response.status_code == 200
    return str(response.content.decode())


def post(client: Client, name: str, target: Any, data: dict[str, Any] | None = None) -> Any:
    return client.post(reverse(f"requests:{name}", args=[target.pk]), data or {}, follow=True)


def messages_of(response: Any) -> list[str]:
    return [str(message) for message in response.context["messages"]]


def number_of(request: MaterialRequest) -> str:
    assert request.number
    return request.number


def refreshed[T: MaterialRequest | MaterialRequestLine](row: T) -> T:
    row.refresh_from_db()
    return row


@pytest.fixture
def approved(make_request: MakeRequest, people: People) -> MaterialRequest:
    return make_request(Status.APPROVED, decided=True, lines=2)


# --- the inbox --------------------------------------------------------------------------


class TestInbox:
    def test_lists_submitted_requests_most_pressing_first(
        self, make_request: MakeRequest, people: People
    ) -> None:
        normal = make_request(Status.SUBMITTED, priority="NORMAL")
        urgent = make_request(Status.SUBMITTED, priority="URGENT")
        high_late = make_request(Status.SUBMITTED, priority="HIGH", needed_by="2099-01-01")
        high_soon = make_request(Status.SUBMITTED, priority="HIGH", needed_by="2098-01-01")
        make_request(Status.DRAFT)
        make_request(Status.APPROVED, decided=True)

        response = client_for(people.mm_mad).get(reverse("requests:inbox"))

        html = response.content.decode()
        assert 'data-testid="inbox"' in html
        assert html.count('data-testid="inbox-row"') == 4
        order = [html.index(number_of(r)) for r in (urgent, high_soon, high_late, normal)]
        assert order == sorted(order)

    def test_filters_by_priority_project_and_deadline(
        self, make_request: MakeRequest, people: People, org: Org
    ) -> None:
        urgent = make_request(Status.SUBMITTED, priority="URGENT", needed_by="2098-01-01")
        other = make_request(Status.SUBMITTED, priority="LOW", project=org.hospital)
        client, url = client_for(people.mm_mad), reverse("requests:inbox")

        by_priority = client.get(url, {"priority": "URGENT"}).content.decode()
        assert number_of(urgent) in by_priority and number_of(other) not in by_priority
        by_project = client.get(url, {"project": str(org.hospital.pk)}).content.decode()
        assert number_of(urgent) in by_project and number_of(other) in by_project
        by_deadline = client.get(url, {"needed_before": "2098-06-01"}).content.decode()
        assert number_of(urgent) in by_deadline and number_of(other) not in by_deadline

    def test_the_approved_tab_follows_up(self, make_request: MakeRequest, people: People) -> None:
        waiting = make_request(Status.SUBMITTED)
        done = make_request(Status.APPROVED, decided=True)

        html = client_for(people.mm_mad).get(reverse("requests:inbox"), {"status": "APPROVED"})

        assert number_of(done) in html.content.decode()
        assert number_of(waiting) not in html.content.decode()

    def test_an_invalid_filter_falls_back_to_the_default(
        self, make_request: MakeRequest, people: People
    ) -> None:
        waiting = make_request(Status.SUBMITTED)
        response = client_for(people.mm_mad).get(reverse("requests:inbox"), {"status": "DRAFT"})
        assert response.status_code == 200 and number_of(waiting) in response.content.decode()

    def test_htmx_answers_the_fragment(self, make_request: MakeRequest, people: People) -> None:
        make_request(Status.SUBMITTED)
        response = client_for(people.mm_mad).get(reverse("requests:inbox"), HTTP_HX_REQUEST="true")
        body = response.content.decode()
        assert 'id="inbox-results"' in body and "<html" not in body

    def test_requests_out_of_scope_are_not_listed(
        self, make_request: MakeRequest, people: People, org: Org
    ) -> None:
        mine = make_request(Status.SUBMITTED)
        foreign = make_request(Status.SUBMITTED, project=org.sev_works)
        html = client_for(people.mm_mad).get(reverse("requests:inbox")).content.decode()
        assert number_of(mine) in html and number_of(foreign) not in html

    @pytest.mark.parametrize("who", ["tech_mad", "wo_mad", "partner"])
    def test_roles_without_review_get_403(self, people: People, who: str) -> None:
        assert client_for(getattr(people, who)).get(reverse("requests:inbox")).status_code == 403


# --- the review page --------------------------------------------------------------------


class TestExaminePage:
    def test_shows_the_lines_and_the_selectors_of_the_e2e_layer(
        self, make_request: MakeRequest, people: People
    ) -> None:
        submitted = make_request(Status.SUBMITTED, lines=2, notes="Urgent for Monday")

        html = page(client_for(people.mm_mad), submitted)

        assert html.count('data-testid="review-line"') == 2
        for selector in ("approve-all", "approve-line", "reject-line", "return-form"):
            assert f'data-testid="{selector}"' in html
        assert 'data-line="1"' in html and "Urgent for Monday" in html

    def test_a_reviewer_cannot_decide_their_own_request(
        self, make_request: MakeRequest, people: People
    ) -> None:
        own = make_request(Status.SUBMITTED, user=people.mm_mad)
        html = page(client_for(people.mm_mad), own)
        assert 'data-testid="self-approval-notice"' in html
        assert 'data-testid="approve-all"' not in html
        assert 'data-testid="approve-line"' not in html

    def test_the_tools_of_an_approved_request(
        self, approved: MaterialRequest, people: People
    ) -> None:
        html = page(client_for(people.mm_mad), approved)
        for selector in (
            "sourcing-form",
            "reduce-form",
            "close-line-form",
            "schedule-form",
            "close-form",
        ):
            assert f'data-testid="{selector}"' in html
        assert 'data-testid="approve-all"' not in html

    def test_a_site_manager_reviews_but_has_no_sourcing_tools(
        self, approved: MaterialRequest, make_request: MakeRequest, people: People
    ) -> None:
        submitted = make_request(Status.SUBMITTED)
        manager = client_for(people.site_manager)
        assert 'data-testid="approve-all"' in page(manager, submitted)
        html = page(manager, approved)
        assert 'data-testid="reduce-form"' in html
        for selector in ("sourcing-form", "close-line-form", "schedule-form", "close-form"):
            assert f'data-testid="{selector}"' not in html

    def test_a_closure_shows_who_why_and_from_where(
        self, approved: MaterialRequest, people: People
    ) -> None:
        line = approved.lines.get(line_no=1)
        services.close_line_short(
            principal_for(people.mm_mad),
            line.pk,
            reason="Supplier discontinued it",
        )
        html = page(client_for(people.mm_mad), approved)
        assert 'data-testid="line-closure"' in html
        assert "Supplier discontinued it" in html and "Line closed" in html
        assert 'data-testid="reopen-line-form"' in html

    def test_a_closed_request_shows_its_closure_and_offers_reopen(
        self, approved: MaterialRequest, people: People
    ) -> None:
        services.manual_close(principal_for(people.mm_mad), approved.pk, reason="Works finished")
        html = page(client_for(people.mm_mad), approved)
        assert 'data-testid="request-closure"' in html and "Works finished" in html
        assert 'data-testid="reopen-form"' in html and 'data-testid="close-form"' not in html

    def test_the_floor_is_shown_for_a_reduction(
        self, approved: MaterialRequest, people: People, flows_of: dict[Any, ledger.Flows]
    ) -> None:
        line = approved.lines.get(line_no=1)
        flows_of[line.pk] = ledger.Flows(delivered=Decimal("2"))
        assert "At least 2 " in page(client_for(people.mm_mad), approved)

    def test_a_non_catalog_line_offers_the_link_dialog(
        self, approved: MaterialRequest, people: People
    ) -> None:
        unit = UnitOfMeasure.objects.get(code="M")
        free = MaterialRequestLine.objects.create(
            request=approved,
            project=approved.project,
            line_no=3,
            free_text="Cable de obra",
            unit=unit,
            requested_qty=Decimal("4"),
            decision=Decision.APPROVED,
            approved_qty=Decimal("4"),
            decided_by=people.mm_mad,
            decided_at=approved.submitted_at,
        )
        html = page(client_for(people.mm_mad), approved)
        assert reverse("requests:link", args=[free.pk]) in html

    def test_plain_users_see_no_review_controls_on_the_status_page(
        self, make_request: MakeRequest, people: People
    ) -> None:
        submitted = make_request(Status.SUBMITTED)
        owner = client_for(people.tech_mad).get(reverse("requests:detail", args=[submitted.pk]))
        assert 'data-testid="open-review"' not in owner.content.decode()
        reviewer = client_for(people.mm_mad).get(reverse("requests:detail", args=[submitted.pk]))
        assert 'data-testid="open-review"' in reviewer.content.decode()

    def test_a_request_out_of_scope_is_404_and_without_review_403(
        self, make_request: MakeRequest, people: People, org: Org
    ) -> None:
        foreign = make_request(Status.SUBMITTED, project=org.sev_works)
        own = make_request(Status.SUBMITTED)
        url = reverse("requests:examine", args=[foreign.pk])
        assert client_for(people.mm_mad).get(url).status_code == 404
        assert (
            client_for(people.tech_mad).get(reverse("requests:examine", args=[own.pk])).status_code
            == 403
        )


# --- decisions --------------------------------------------------------------------------


class TestDecisions:
    def test_approving_a_line_with_an_adjusted_quantity(
        self, make_request: MakeRequest, people: People
    ) -> None:
        submitted = make_request(Status.SUBMITTED)
        line = submitted.lines.get()

        response = post(
            client_for(people.mm_mad),
            "decide_line",
            line,
            {"decision": "APPROVED", "approved_qty": "7", "note": ""},
        )

        assert "Line 1 decided." in messages_of(response)
        line = refreshed(line)
        assert line.decision == Decision.APPROVED and line.approved_qty == Decimal("7")
        assert line.requested_qty == Decimal("10")  # both values are kept
        assert refreshed(submitted).status == Status.APPROVED  # the last line finalises

    def test_rejecting_needs_a_reason(self, make_request: MakeRequest, people: People) -> None:
        line = make_request(Status.SUBMITTED).lines.get()
        client = client_for(people.mm_mad)

        refused = post(client, "decide_line", line, {"decision": "REJECTED", "note": ""})
        assert any("Give a reason" in message for message in messages_of(refused))
        assert refreshed(line).decision == Decision.PENDING

        post(client, "decide_line", line, {"decision": "REJECTED", "note": "Not needed"})
        line = refreshed(line)
        assert line.decision == Decision.REJECTED and line.decision_note == "Not needed"
        assert refreshed(line.request).status == Status.REJECTED

    def test_more_than_requested_is_refused_with_the_services_message(
        self, make_request: MakeRequest, people: People
    ) -> None:
        line = make_request(Status.SUBMITTED).lines.get()
        response = post(
            client_for(people.mm_mad),
            "decide_line",
            line,
            {"decision": "APPROVED", "approved_qty": "11"},
        )
        assert any("At most the requested quantity" in message for message in messages_of(response))
        assert refreshed(line).decision == Decision.PENDING

    def test_approve_all_finalises(self, make_request: MakeRequest, people: People) -> None:
        submitted = make_request(Status.SUBMITTED, lines=3)
        response = post(client_for(people.mm_mad), "approve_all", submitted)
        assert "Every pending line is approved." in messages_of(response)
        assert refreshed(submitted).status == Status.APPROVED
        assert not submitted.lines.exclude(decision=Decision.APPROVED).exists()

    def test_return_for_changes_clears_decisions_and_needs_a_note(
        self, make_request: MakeRequest, people: People
    ) -> None:
        submitted = make_request(Status.SUBMITTED, lines=2)
        client = client_for(people.mm_mad)
        first = submitted.lines.get(line_no=1)
        services.decide_line(principal_for(people.mm_mad), first.pk, decision="APPROVED")

        refused = post(client, "return_to_draft", submitted, {"note": ""})
        assert refreshed(submitted).status == Status.SUBMITTED
        assert refused.redirect_chain[-1][0] == reverse("requests:examine", args=[submitted.pk])

        done = post(client, "return_to_draft", submitted, {"note": "Add the cable size"})
        assert done.redirect_chain[-1][0] == reverse("requests:inbox")
        assert refreshed(submitted).status == Status.DRAFT
        assert not submitted.lines.exclude(decision=Decision.PENDING).exists()

    def test_a_service_error_is_flashed_not_raised(
        self, make_request: MakeRequest, people: People
    ) -> None:
        draft = make_request(Status.DRAFT)
        response = post(client_for(people.mm_mad), "approve_all", draft)
        assert any("cannot be changed this way" in message for message in messages_of(response))

    def test_self_approval_is_refused_by_the_service(
        self, make_request: MakeRequest, people: People
    ) -> None:
        own = make_request(Status.SUBMITTED, user=people.mm_mad)
        response = client_for(people.mm_mad).post(reverse("requests:approve_all", args=[own.pk]))
        assert response.status_code == 403
        assert refreshed(own).status == Status.SUBMITTED

    def test_another_reviewers_decision_is_kept(
        self, make_request: MakeRequest, people: People
    ) -> None:
        submitted = make_request(Status.SUBMITTED, lines=2)
        first = submitted.lines.get(line_no=1)
        services.decide_line(principal_for(people.mm_mad), first.pk, decision="APPROVED")

        html = page(client_for(people.site_manager), submitted)

        assert html.count('data-testid="decide-form"') == 1  # only the pending line
        response = post(
            client_for(people.site_manager),
            "decide_line",
            first,
            {"decision": "REJECTED", "note": "x"},
        )
        assert any("decided by another reviewer" in message for message in messages_of(response))

    def test_decisions_are_recorded_and_shown_in_the_history(
        self, make_request: MakeRequest, people: People
    ) -> None:
        submitted = make_request(Status.SUBMITTED)
        line = submitted.lines.get()
        post(
            client_for(people.mm_mad),
            "decide_line",
            line,
            {"decision": "APPROVED", "approved_qty": "7", "note": "Fine"},
        )
        html = page(client_for(people.mm_mad), submitted)
        assert 'data-action="request_line.decided"' in html
        assert 'data-action="request.approved"' in html
        assert people.mm_mad.email in html and "Approved · 7 · Fine" in html


# --- the material manager's tools -------------------------------------------------------


class TestSourcingTools:
    def test_reduce_within_the_floor(
        self, approved: MaterialRequest, people: People, flows_of: dict[Any, ledger.Flows]
    ) -> None:
        line = approved.lines.get(line_no=1)
        flows_of[line.pk] = ledger.Flows(delivered=Decimal("3"))
        client = client_for(people.mm_mad)

        refused = post(client, "reduce_line", line, {"approved_qty": "2", "reason": ""})
        assert any("already delivered" in message for message in messages_of(refused))
        assert refreshed(line).approved_qty == Decimal("5")

        post(client, "reduce_line", line, {"approved_qty": "4", "reason": "Overestimated"})
        assert refreshed(line).approved_qty == Decimal("4")
        assert "5 → 4 · Overestimated" in page(client, approved)

    def test_set_sourcing(self, approved: MaterialRequest, people: People) -> None:
        line = approved.lines.get(line_no=1)
        post(client_for(people.mm_mad), "set_sourcing", line, {"sourcing": "PURCHASE"})
        assert refreshed(line).sourcing == "PURCHASE"
        bad = post(client_for(people.mm_mad), "set_sourcing", line, {"sourcing": "UNDECIDED"})
        assert messages_of(bad) and refreshed(line).sourcing == "PURCHASE"

    def test_close_short_and_reopen_a_line(self, approved: MaterialRequest, people: People) -> None:
        line = approved.lines.get(line_no=1)
        client = client_for(people.mm_mad)

        missing = post(client, "close_line", line, {"reason": ""})
        assert any("Give a reason" in message for message in messages_of(missing))
        assert refreshed(line).closed_short_at is None

        post(client, "close_line", line, {"reason": "Not available"})
        closed = refreshed(line)
        assert closed.closed_short_at and closed.closed_short_source == "LINE"
        assert not ledger.is_live(closed)

        post(client, "reopen_line", line, {"reason": "Found stock"})
        assert refreshed(line).closed_short_at is None

    def test_schedule_changes_priority_and_clears_the_date(
        self, approved: MaterialRequest, people: People
    ) -> None:
        client = client_for(people.mm_mad)
        post(client, "schedule", approved, {"priority": "URGENT", "needed_by": "2099-05-01"})
        approved = refreshed(approved)
        assert approved.priority == "URGENT" and str(approved.needed_by) == "2099-05-01"
        post(client, "schedule", approved, {"priority": "URGENT", "needed_by": ""})
        assert refreshed(approved).needed_by is None
        events = AuditEvent.objects.filter(action="request.rescheduled")
        assert events.count() == 2

    def test_manual_close_then_reopen_restores_only_its_own_lines(
        self, approved: MaterialRequest, people: People
    ) -> None:
        client = client_for(people.mm_mad)
        own_line = approved.lines.get(line_no=1)
        post(client, "close_line", own_line, {"reason": "Cancelled by site"})

        post(client, "close", approved, {"reason": "Works finished"})
        assert refreshed(approved).status == Status.CLOSED

        post(client, "reopen", approved, {"reason": "More work"})
        assert refreshed(approved).status == Status.APPROVED
        assert refreshed(own_line).closed_short_at is not None  # closed on its own: stays
        assert refreshed(approved.lines.get(line_no=2)).closed_short_at is None

    def test_cancelling_somebody_elses_request(
        self, make_request: MakeRequest, people: People
    ) -> None:
        submitted = make_request(Status.SUBMITTED)
        html = page(client_for(people.mm_mad), submitted)
        assert 'data-testid="cancel-form"' in html
        post(client_for(people.mm_mad), "cancel_any", submitted, {"reason": "Duplicate"})
        assert refreshed(submitted).status == Status.CANCELLED

    def test_cancel_is_not_offered_on_ones_own_request(
        self, make_request: MakeRequest, people: People
    ) -> None:
        own = make_request(Status.SUBMITTED, user=people.mm_mad)
        assert 'data-testid="cancel-form"' not in page(client_for(people.mm_mad), own)

    def test_a_site_manager_cannot_use_the_sourcing_urls(
        self, approved: MaterialRequest, people: People
    ) -> None:
        line = approved.lines.get(line_no=1)
        client = client_for(people.site_manager)
        for name, target in (("set_sourcing", line), ("close_line", line), ("close", approved)):
            sent = client.post(reverse(f"requests:{name}", args=[target.pk]), {"reason": "x"})
            assert sent.status_code == 403, name


class TestLinkDialog:
    @pytest.fixture
    def free_line(self, approved: MaterialRequest, people: People) -> MaterialRequestLine:
        return MaterialRequestLine.objects.create(
            request=approved,
            project=approved.project,
            line_no=3,
            free_text="Cable de obra",
            unit=UnitOfMeasure.objects.get(code="M"),
            requested_qty=Decimal("4"),
            decision=Decision.APPROVED,
            approved_qty=Decimal("4"),
            decided_by=people.mm_mad,
            decided_at=approved.submitted_at,
        )

    def test_search_shows_only_materials_in_the_lines_unit(
        self, free_line: MaterialRequestLine, people: People, cable: Material, breaker: Material
    ) -> None:
        response = client_for(people.mm_mad).get(
            reverse("requests:link", args=[free_line.pk]), {"q": "ca"}
        )
        html = response.content.decode()
        assert 'data-material="CAB-25"' in html  # metres
        assert 'data-material="DIF-40"' not in html  # units: another unit

    def test_linking_keeps_the_description(
        self, free_line: MaterialRequestLine, people: People, cable: Material
    ) -> None:
        response = post(
            client_for(people.mm_mad), "resolve_line", free_line, {"material": str(cable.pk)}
        )
        assert "Line linked to the catalog." in messages_of(response)
        free_line = refreshed(free_line)
        assert free_line.material_id == cable.pk and free_line.free_text == "Cable de obra"

    def test_another_unit_is_refused(
        self, free_line: MaterialRequestLine, people: People, breaker: Material
    ) -> None:
        response = post(
            client_for(people.mm_mad), "resolve_line", free_line, {"material": str(breaker.pk)}
        )
        assert any("another unit" in message for message in messages_of(response))
        assert refreshed(free_line).material_id is None

    def test_a_catalog_line_has_no_dialog(self, approved: MaterialRequest, people: People) -> None:
        line = approved.lines.get(line_no=1)
        response = client_for(people.mm_mad).get(reverse("requests:link", args=[line.pk]))
        assert response.status_code == 302


class TestHistory:
    def test_is_hidden_from_who_neither_reviews_nor_audits(
        self, make_request: MakeRequest, people: People
    ) -> None:
        submitted = make_request(Status.SUBMITTED)
        services.approve_all(principal_for(people.mm_mad), submitted.pk)
        # The requester's status page shows lines and notes, never the audit trail.
        owner = client_for(people.tech_mad).get(reverse("requests:detail", args=[submitted.pk]))
        assert "request.approved" not in owner.content.decode()
        assert 'data-testid="history"' not in owner.content.decode()

    def test_lists_events_oldest_first_with_their_reasons(
        self, approved: MaterialRequest, people: People
    ) -> None:
        services.manual_close(principal_for(people.mm_mad), approved.pk, reason="Works finished")
        html = page(client_for(people.mm_mad), approved)
        assert 'data-action="request.closed"' in html and "Works finished" in html

    def test_the_selector_gives_nothing_to_who_may_not_see_it(
        self, make_request: MakeRequest, people: People
    ) -> None:
        submitted = make_request(Status.SUBMITTED)
        services.approve_all(principal_for(people.mm_mad), submitted.pk)
        assert selectors.history(AccessContext.for_user(people.tech_mad), submitted) == []
        assert selectors.history(AccessContext.for_user(people.mm_mad), submitted)


class TestBusyDatabase:
    @pytest.mark.parametrize(
        ("name", "service"),
        [
            ("approve_all", "approve_all"),
            ("close", "manual_close"),
            ("cancel_any", "cancel"),
        ],
    )
    def test_a_service_that_gives_up_answers_409(
        self,
        monkeypatch: pytest.MonkeyPatch,
        make_request: MakeRequest,
        people: People,
        name: str,
        service: str,
    ) -> None:
        def give_up(*args: Any, **kwargs: Any) -> None:
            raise RetryExhausted("deadlocks")

        monkeypatch.setattr(services, service, give_up)
        submitted = make_request(Status.SUBMITTED)
        response = client_for(people.mm_mad).post(
            reverse(f"requests:{name}", args=[submitted.pk]), {"reason": "x"}
        )
        assert response.status_code == 409

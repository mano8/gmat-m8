"""The review workflow screens (M7b.4): the reviewer picker at submit, "Assigned to me", the
assignee and the reassign dialog, the edit tools, the page version, and what the requester
reads of the edits.

Every action is a POST-only URL that redirects back to the review page with a flash message.
The sentinel supplier and price must not reach the requester's page even after an edit.
"""

from decimal import Decimal
from typing import Any

import pytest
from django.test import Client
from django.urls import reverse

from apps.access.context import AccessContext
from apps.audit.models import AuditEvent
from apps.catalog.models import Material
from apps.material_requests import selectors, services
from apps.material_requests.forms import SubmitForm
from apps.material_requests.models import (
    MaterialRequest,
    MaterialRequestLine,
    MaterialRequestLineRevision,
)
from tests.org import Org, People, Sentinel
from tests.org import client_for as signed_in
from tests.support import principal_for
from tests.unit.material_requests.conftest import MakeRequest

pytestmark = pytest.mark.django_db

Status = MaterialRequest.Status
Decision = MaterialRequestLine.Decision
Kind = MaterialRequestLineRevision.Kind


def client_for(user: Any) -> Client:
    return signed_in(user, HTTP_ACCEPT_LANGUAGE="en")


def url(name: str, target: Any) -> str:
    return reverse(f"requests:{name}", args=[target.pk])


def html(response: Any) -> str:
    return str(response.content.decode())


def number_of(request: MaterialRequest) -> str:
    assert request.number
    return request.number


def messages_of(response: Any) -> list[str]:
    return [str(message) for message in response.context["messages"]]


def submitted(make_request: MakeRequest, people: People, lines: int = 2) -> MaterialRequest:
    """The technician's request, submitted through the service (so it has its copy)."""
    draft = make_request(lines=lines)
    return services.submit(principal_for(people.tech_mad), draft.pk)


def approved(make_request: MakeRequest, people: People, lines: int = 2) -> MaterialRequest:
    return services.approve_all(
        principal_for(people.mm_mad), submitted(make_request, people, lines).pk
    )


def dialog(client: Client, target: str, **params: str) -> Any:
    response = client.get(target, params)
    assert response.status_code == 200, response.status_code
    return response


def save(client: Client, response: Any, **data: str) -> Any:
    """Post the dialog's own form (its key and page version) with ``data``."""
    form = response.context["form"]
    payload = {
        "idempotency_key": form.initial["idempotency_key"],
        "seen_version": form.initial["seen_version"],
        **data,
    }
    return client.post(response.context["action"], payload, follow=True)


# --- the reviewer picker at submit ---------------------------------------------------------


class TestSubmitPicker:
    def test_the_review_step_offers_the_reviewers_of_the_branch(
        self, make_request: MakeRequest, people: People
    ) -> None:
        draft = make_request()
        page = html(client_for(people.tech_mad).get(url("review", draft)))
        assert 'name="reviewer"' in page
        for offered in (people.mm_mad, people.engineer, people.group_admin):
            assert str(offered.pk) in page
        assert str(people.mm_sev.pk) not in page  # another branch's manager
        assert str(people.site_manager.pk) not in page  # not assignable

    def test_a_picked_reviewer_gets_the_request_and_the_requester_sees_who(
        self, make_request: MakeRequest, people: People
    ) -> None:
        draft, client = make_request(), client_for(people.tech_mad)
        review = client.get(url("review", draft))
        response = client.post(
            url("submit", draft),
            {
                "priority": "NORMAL",
                "idempotency_key": review.context["form"].initial["idempotency_key"],
                "reviewer": str(people.engineer.pk),
            },
            follow=True,
        )
        draft.refresh_from_db()
        assert draft.assigned_reviewer == people.engineer
        assert 'data-testid="detail-assignee"' in html(response)

    def test_nobody_in_particular_leaves_the_request_unassigned(
        self, make_request: MakeRequest, people: People
    ) -> None:
        draft, client = make_request(), client_for(people.tech_mad)
        review = client.get(url("review", draft))
        client.post(
            url("submit", draft),
            {
                "priority": "NORMAL",
                "idempotency_key": review.context["form"].initial["idempotency_key"],
                "reviewer": "",
            },
        )
        draft.refresh_from_db()
        assert draft.status == Status.SUBMITTED and draft.assigned_reviewer is None

    def test_a_reviewer_of_another_branch_is_not_a_choice(
        self, make_request: MakeRequest, people: People
    ) -> None:
        draft, client = make_request(), client_for(people.tech_mad)
        review = client.get(url("review", draft))
        response = client.post(
            url("submit", draft),
            {
                "priority": "NORMAL",
                "idempotency_key": review.context["form"].initial["idempotency_key"],
                "reviewer": str(people.mm_sev.pk),
            },
        )
        assert response.status_code == 200 and "Select a valid choice" in html(response)
        draft.refresh_from_db()
        assert draft.status == Status.DRAFT

    def test_a_resubmit_keeps_the_reviewer_unless_another_is_picked(
        self, make_request: MakeRequest, people: People
    ) -> None:
        draft, client = make_request(), client_for(people.tech_mad)
        services.submit(principal_for(people.tech_mad), draft.pk, reviewer_id=people.engineer.pk)
        services.return_to_draft(principal_for(people.mm_mad), draft.pk, note="Add the size")
        review = client.get(url("review", draft))
        form = review.context["form"]
        assert "" not in dict(form.fields["reviewer"].choices)  # no unassigning (ADR-0014 §4)
        assert str(form.initial["reviewer"]) == str(people.engineer.pk)

    def test_without_candidates_the_form_has_no_picker(self) -> None:
        form = SubmitForm(reviewers=[])
        assert "reviewer" not in form.fields


# --- "Assigned to me" -----------------------------------------------------------------------


class TestAssignedToMe:
    def test_the_filter_keeps_the_requests_sent_to_the_user(
        self, make_request: MakeRequest, people: People
    ) -> None:
        mine = submitted(make_request, people)
        other = submitted(make_request, people)
        services.assign_reviewer(
            principal_for(people.mm_mad), mine.pk, reviewer_id=people.engineer.pk, note="Yours"
        )
        client = client_for(people.engineer)
        everything = html(client.get(reverse("requests:inbox")))
        filtered = html(client.get(reverse("requests:inbox"), {"assigned": "1"}))
        assert number_of(mine) in everything and number_of(other) in everything
        assert number_of(mine) in filtered and number_of(other) not in filtered
        assert "Assigned to me" in everything  # the chip of the row

    def test_the_inbox_filter_works_on_approved_requests_too(
        self, make_request: MakeRequest, people: People
    ) -> None:
        found = approved(make_request, people)
        services.assign_reviewer(
            principal_for(people.mm_mad), found.pk, reviewer_id=people.engineer.pk, note="Follow"
        )
        ctx = AccessContext.for_user(people.engineer)
        assert list(selectors.inbox(ctx, status=Status.APPROVED, assigned_to_me=True)) == [found]
        assert list(selectors.inbox(ctx, status=Status.SUBMITTED, assigned_to_me=True)) == []

    def test_rejected_requests_sent_to_the_user_are_listed_too(
        self, make_request: MakeRequest, people: People
    ) -> None:
        found = submitted(make_request, people, lines=1)
        services.assign_reviewer(
            principal_for(people.mm_mad), found.pk, reviewer_id=people.engineer.pk, note="Yours"
        )
        line = found.lines.get()
        services.decide_line(
            principal_for(people.mm_mad), line.pk, decision="REJECTED", note="Duplicate"
        )
        client = client_for(people.engineer)
        page = html(client.get(reverse("requests:inbox"), {"assigned": "1", "status": "REJECTED"}))
        assert number_of(found) in page and "Assigned to me" in page

    def test_a_lapsed_assignee_has_an_empty_list(
        self, make_request: MakeRequest, people: People
    ) -> None:
        found = submitted(make_request, people)
        services.assign_reviewer(
            principal_for(people.mm_mad), found.pk, reviewer_id=people.engineer.pk, note="Yours"
        )
        MaterialRequest.objects.filter(pk=found.pk).update(assigned_reviewer=people.site_manager)
        page = html(
            client_for(people.site_manager).get(reverse("requests:inbox"), {"assigned": "1"})
        )
        assert number_of(found) not in page


# --- the assignee and the reassign dialog ---------------------------------------------------


class TestAssignment:
    def test_the_page_names_the_assignee_or_nobody(
        self, make_request: MakeRequest, people: People
    ) -> None:
        found, client = submitted(make_request, people), client_for(people.mm_mad)
        assert "Nobody in particular" in html(client.get(url("examine", found)))
        services.assign_reviewer(
            principal_for(people.mm_mad), found.pk, reviewer_id=people.engineer.pk, note="Yours"
        )
        page = html(client.get(url("examine", found)))
        assert 'data-testid="assignee"' in page and people.engineer.email in page

    def test_a_reviewer_reassigns_with_a_note(
        self, make_request: MakeRequest, people: People
    ) -> None:
        found, client = submitted(make_request, people), client_for(people.mm_mad)
        response = client.post(
            url("assign_reviewer", found),
            {"reviewer": str(people.engineer.pk), "note": "You know this site"},
            follow=True,
        )
        assert "Request sent to the reviewer." in messages_of(response)
        found.refresh_from_db()
        assert found.assigned_reviewer == people.engineer
        event = AuditEvent.objects.get(action="request.reviewer_assigned")
        assert event.data["note"] == "You know this site"
        assert "Sent to a reviewer" in html(response)  # the timeline label

    def test_the_note_is_required_and_the_choice_comes_from_the_selector(
        self, make_request: MakeRequest, people: People
    ) -> None:
        found, client = submitted(make_request, people), client_for(people.mm_mad)
        no_note = client.post(
            url("assign_reviewer", found), {"reviewer": str(people.engineer.pk)}, follow=True
        )
        assert "Give a note." in " ".join(messages_of(no_note))
        elsewhere = client.post(
            url("assign_reviewer", found),
            {"reviewer": str(people.mm_sev.pk), "note": "x"},
            follow=True,
        )
        assert messages_of(elsewhere)
        found.refresh_from_db()
        assert found.assigned_reviewer is None

    def test_the_current_assignee_is_not_offered_again(
        self, make_request: MakeRequest, people: People
    ) -> None:
        found, client = submitted(make_request, people), client_for(people.mm_mad)
        services.assign_reviewer(
            principal_for(people.mm_mad), found.pk, reviewer_id=people.engineer.pk, note="Yours"
        )
        response = client.get(url("examine", found))
        choices = dict(response.context["assign_form"].fields["reviewer"].choices)
        assert str(people.engineer.pk) not in choices and str(people.mm_mad.pk) in choices
        again = client.post(
            url("assign_reviewer", found),
            {"reviewer": str(people.engineer.pk), "note": "Again"},
            follow=True,
        )
        assert "Select a valid choice" in " ".join(messages_of(again))
        assert AuditEvent.objects.filter(action="request.reviewer_assigned").count() == 1

    def test_a_site_manager_may_reassign_but_not_be_assigned(
        self, make_request: MakeRequest, people: People
    ) -> None:
        found, client = submitted(make_request, people), client_for(people.site_manager)
        page = html(client.get(url("examine", found)))
        assert 'data-testid="assign-form"' in page
        assert str(people.site_manager.pk) not in page.split('data-testid="assign-form"')[1]


# --- the page version -----------------------------------------------------------------------


class TestPageVersion:
    def test_the_decision_forms_carry_the_version_and_a_stale_page_is_refused(
        self, make_request: MakeRequest, people: People
    ) -> None:
        found, client = submitted(make_request, people), client_for(people.mm_mad)
        response = client.get(url("examine", found))
        version = selectors.version(found)
        assert page_has_version(html(response), version)
        line = found.lines.get(line_no=1)
        services.edit_line(
            principal_for(people.engineer),
            line.pk,
            services.LineEdit("More", {"requested_qty": Decimal("12")}),
        )
        stale = client.post(url("approve_all", found), {"seen_version": str(version)}, follow=True)
        assert "changed after you opened it" in " ".join(messages_of(stale))
        found.refresh_from_db()
        assert found.status == Status.SUBMITTED
        decided = client.post(
            url("decide_line", line),
            {"decision": "APPROVED", "approved_qty": "12", "seen_version": str(version)},
            follow=True,
        )
        assert "changed after you opened it" in " ".join(messages_of(decided))
        fresh = client.post(
            url("approve_all", found),
            {"seen_version": str(selectors.version(found))},
            follow=True,
        )
        assert "Every pending line is approved." in messages_of(fresh)

    def test_a_form_without_the_version_is_refused_not_left_unchecked(
        self, make_request: MakeRequest, people: People
    ) -> None:
        found, client = submitted(make_request, people), client_for(people.mm_mad)
        refused = client.post(url("approve_all", found), {}, follow=True)
        assert "This page is out of date: reload it." in " ".join(messages_of(refused))
        line = found.lines.get(line_no=1)
        undecided = client.post(url("decide_line", line), {"decision": "APPROVED"}, follow=True)
        assert "out of date" in " ".join(messages_of(undecided))
        found.refresh_from_db()
        assert found.status == Status.SUBMITTED
        assert not found.lines.exclude(decision=Decision.PENDING).exists()


def page_has_version(page: str, version: int) -> bool:
    return page.count(f'name="seen_version" value="{version}"') >= 2


# --- the edit tools -------------------------------------------------------------------------


class TestEditTools:
    def test_a_submitted_request_offers_change_remove_and_add(
        self, make_request: MakeRequest, people: People
    ) -> None:
        found = submitted(make_request, people)
        page = html(client_for(people.engineer).get(url("examine", found)))
        assert page.count('data-testid="edit-line"') == 2
        assert page.count('data-testid="remove-line-form"') == 2
        assert 'data-testid="add-line"' in page

    def test_who_cannot_edit_is_not_offered_the_tools(
        self, make_request: MakeRequest, people: People
    ) -> None:
        found = submitted(make_request, people)
        page = html(client_for(people.site_manager).get(url("examine", found)))
        assert "edit-line" not in page and "add-line" not in page and "remove-line" not in page

    def test_changing_a_quantity_and_saving_and_approving(
        self, make_request: MakeRequest, people: People
    ) -> None:
        found, client = submitted(make_request, people), client_for(people.engineer)
        line = found.lines.get(line_no=1)
        page = dialog(client, url("edit_line", line))
        assert "Save and approve" in html(page)
        response = save(
            client, page, requested_qty="12", reason="Site measured again", decision="APPROVED"
        )
        assert "Line 1 changed." in messages_of(response)
        line.refresh_from_db()
        assert line.requested_qty == Decimal("12")
        assert (line.decision, line.approved_qty) == (Decision.APPROVED, Decimal("12"))
        assert 'data-testid="line-edits"' in html(response)
        revision = MaterialRequestLineRevision.objects.get()
        assert (revision.kind, revision.reason) == (Kind.CHANGE, "Site measured again")

    def test_a_plain_save_leaves_the_line_for_another_decision(
        self, make_request: MakeRequest, people: People
    ) -> None:
        found, client = submitted(make_request, people), client_for(people.engineer)
        line = found.lines.get(line_no=1)
        response = save(
            client, dialog(client, url("edit_line", line)), requested_qty="9", reason="Fewer"
        )
        line.refresh_from_db()
        assert line.decision == Decision.PENDING and line.requested_qty == Decimal("9")
        assert "Line 1 changed." in messages_of(response)

    def test_the_reason_is_required_and_nothing_changes_without_one(
        self, make_request: MakeRequest, people: People
    ) -> None:
        found, client = submitted(make_request, people), client_for(people.engineer)
        line = found.lines.get(line_no=1)
        response = save(client, dialog(client, url("edit_line", line)), requested_qty="9")
        assert "Give a reason." in " ".join(messages_of(response))
        assert not MaterialRequestLineRevision.objects.exists()

    def test_a_service_refusal_comes_back_to_the_dialog(
        self, make_request: MakeRequest, people: People
    ) -> None:
        found, client = submitted(make_request, people), client_for(people.engineer)
        line = found.lines.get(line_no=1)
        response = save(client, dialog(client, url("edit_line", line)), reason="Nothing")
        assert "Change something before saving." in " ".join(messages_of(response))
        assert response.redirect_chain[-1][0].startswith(url("edit_line", line))

    def test_a_double_tap_makes_one_revision(
        self, make_request: MakeRequest, people: People
    ) -> None:
        found, client = submitted(make_request, people), client_for(people.engineer)
        line = found.lines.get(line_no=1)
        page = dialog(client, url("edit_line", line))
        first = save(client, page, requested_qty="11", reason="Twice")
        second = save(client, page, requested_qty="11", reason="Twice")
        assert first.status_code == second.status_code == 200
        assert MaterialRequestLineRevision.objects.count() == 1

    def test_the_material_is_searched_chosen_and_kept(
        self, make_request: MakeRequest, people: People, breaker: Material
    ) -> None:
        found, client = submitted(make_request, people), client_for(people.engineer)
        line = found.lines.get(line_no=1)
        target = url("edit_line", line)
        results = html(dialog(client, target, q="Diferencial"))
        assert 'data-testid="material-candidates"' in results and breaker.reference in results
        picked = dialog(client, target, material=str(breaker.pk))
        assert 'data-testid="chosen-material"' in html(picked)
        response = save(
            client,
            picked,
            material=str(breaker.pk),
            requested_qty="4",
            reason="Breaker, not cable",
        )
        line.refresh_from_db()
        assert line.material == breaker and line.unit == breaker.unit
        assert "Line 1 changed." in messages_of(response)

    def test_a_partly_approved_line_is_raised_back_to_its_request(
        self, make_request: MakeRequest, people: People
    ) -> None:
        found, client = approved(make_request, people), client_for(people.engineer)
        line = found.lines.get(line_no=1)
        services.reduce_approved_qty(
            principal_for(people.mm_mad), line.pk, approved_qty=Decimal("6"), reason="Stock"
        )
        page = dialog(client, url("edit_line", line))
        assert page.context["form"].initial["requested_qty"] == Decimal("6")  # what is approved
        response = save(client, page, requested_qty="10", reason="Supplier has it again")
        assert "Line 1 changed." in messages_of(response)
        line.refresh_from_db()
        assert (line.requested_qty, line.approved_qty) == (Decimal("10"), Decimal("10"))

    def test_another_unit_asks_for_the_quantity_again(
        self, make_request: MakeRequest, people: People, breaker: Material
    ) -> None:
        found, client = submitted(make_request, people), client_for(people.engineer)
        line = found.lines.get(line_no=1)
        page = dialog(client, url("edit_line", line), material=str(breaker.pk))
        assert page.context["form"].initial["requested_qty"] is None  # not carried over
        response = save(client, page, material=str(breaker.pk), reason="Breaker")
        assert "Give a quantity." in " ".join(messages_of(response))
        line.refresh_from_db()
        assert line.material_id != breaker.pk

    def test_an_unknown_material_in_the_link_is_ignored(
        self, make_request: MakeRequest, people: People
    ) -> None:
        found, client = submitted(make_request, people), client_for(people.engineer)
        line = found.lines.get(line_no=1)
        page = dialog(client, url("edit_line", line), material="not-an-id")
        assert 'data-testid="chosen-material"' not in html(page)

    def test_an_approved_request_is_changed_removed_and_added_to(
        self, make_request: MakeRequest, people: People, breaker: Material
    ) -> None:
        found, client = approved(make_request, people), client_for(people.engineer)
        first, second = found.lines.order_by("line_no")
        page = dialog(client, url("edit_line", first))
        assert "Save and approve" not in html(page)
        changed = save(client, page, requested_qty="14", reason="Supplier stock")
        assert "Line 1 changed." in messages_of(changed)
        first.refresh_from_db()
        assert (first.requested_qty, first.approved_qty) == (Decimal("14"), Decimal("14"))

        remove = changed.context["rows"][1].remove.form.initial
        removed = client.post(
            url("remove_reviewed_line", second),
            {
                "reason": "Not needed",
                "idempotency_key": remove["idempotency_key"],
                "seen_version": remove["seen_version"],
            },
            follow=True,
        )
        assert "Line 2 removed." in messages_of(removed)
        second.refresh_from_db()
        assert second.closed_short_at is not None
        assert f'data-line="{second.line_no}"' in html(removed)  # it stays visible

        add = dialog(client, url("new_line", found), material=str(breaker.pk))
        added = save(client, add, material=str(breaker.pk), requested_qty="3", reason="Also")
        assert "Line added." in messages_of(added)
        new = found.lines.get(line_no=3)
        assert (new.material, new.decision) == (breaker, Decision.APPROVED)

    def test_a_submitted_line_is_rejected_by_removing_it(
        self, make_request: MakeRequest, people: People
    ) -> None:
        found, client = submitted(make_request, people), client_for(people.engineer)
        line = found.lines.get(line_no=2)
        response = client.get(url("examine", found))
        remove = response.context["rows"][1].remove.form.initial
        removed = client.post(
            url("remove_reviewed_line", line),
            {
                "reason": "Duplicate",
                "idempotency_key": remove["idempotency_key"],
                "seen_version": remove["seen_version"],
            },
            follow=True,
        )
        assert "Line 2 removed." in messages_of(removed)
        line.refresh_from_db()
        assert (line.decision, line.decision_note) == (Decision.REJECTED, "Duplicate")
        assert 'data-testid="remove-line-form"' in html(removed)  # line 1's, not line 2's
        assert html(removed).count('data-testid="remove-line-form"') == 1

    def test_a_line_added_in_a_submitted_request_can_be_approved_at_once(
        self, make_request: MakeRequest, people: People, breaker: Material
    ) -> None:
        found, client = submitted(make_request, people), client_for(people.engineer)
        page = dialog(client, url("new_line", found))
        response = save(
            client,
            page,
            free_text="Cinta aislante",
            unit=str(breaker.unit_id),
            requested_qty="2",
            reason="Missing from the list",
            decision="APPROVED",
        )
        assert "Line added." in messages_of(response)
        assert found.lines.get(line_no=3).decision == Decision.APPROVED

    def test_a_new_line_needs_a_quantity(self, make_request: MakeRequest, people: People) -> None:
        found, client = submitted(make_request, people), client_for(people.engineer)
        response = save(
            client, dialog(client, url("new_line", found)), free_text="Tape", reason="Missing"
        )
        assert "Give a quantity." in " ".join(messages_of(response))

    def test_the_dialog_refuses_a_request_that_cannot_be_edited(
        self, make_request: MakeRequest, people: People
    ) -> None:
        client = client_for(people.engineer)
        closed = make_request(Status.CLOSED, decided=True)
        refused = client.get(url("new_line", closed), follow=True)
        assert "This line can no longer be edited." in messages_of(refused)
        live = approved(make_request, people)
        line = live.lines.get(line_no=1)
        services.close_line_short(principal_for(people.mm_mad), line.pk, reason="Gone")
        gone = client.get(url("edit_line", line), follow=True)
        assert "This line can no longer be edited." in messages_of(gone)


# --- what the requester reads ----------------------------------------------------------------


class TestRequesterSeesTheEdits:
    def test_the_technician_sees_the_original_and_the_edits(
        self, make_request: MakeRequest, people: People, breaker: Material
    ) -> None:
        found = submitted(make_request, people)
        engineer = client_for(people.engineer)
        first, second = found.lines.order_by("line_no")
        save(
            engineer,
            dialog(engineer, url("edit_line", first)),
            requested_qty="12",
            reason="Measured on site",
        )
        remove = engineer.get(url("examine", found)).context["rows"][1].remove.form.initial
        engineer.post(
            url("remove_reviewed_line", second),
            {
                "reason": "Not needed",
                "idempotency_key": remove["idempotency_key"],
                "seen_version": remove["seen_version"],
            },
        )
        save(
            engineer,
            dialog(engineer, url("new_line", found), material=str(breaker.pk)),
            material=str(breaker.pk),
            requested_qty="2",
            reason="Also needed",
        )

        page = html(client_for(people.tech_mad).get(url("detail", found)))
        assert 'data-testid="request-changes"' in page
        assert page.count('data-testid="changed-line"') == 3
        assert "As submitted" in page and "10" in page  # the original quantity
        assert "Measured on site" in page and "Not needed" in page and "Also needed" in page
        assert "Added by a reviewer" in page
        assert people.engineer.email in page or people.engineer.full_name in page
        assert page.count("Edited") >= 1  # the marker beside the line
        assert 'data-line="2"' in page  # the removed line stays visible

    def test_a_request_nobody_edited_shows_no_changes(
        self, make_request: MakeRequest, people: People
    ) -> None:
        found = submitted(make_request, people)
        page = html(client_for(people.tech_mad).get(url("detail", found)))
        assert "request-changes" not in page

    def test_the_requester_page_never_shows_prices_suppliers_or_other_projects(
        self, org: Org, make_request: MakeRequest, people: People
    ) -> None:
        found = approved(make_request, people)
        line = found.lines.get(line_no=1)
        engineer = client_for(people.engineer)
        save(
            engineer,
            dialog(engineer, url("edit_line", line)),
            requested_qty="14",
            reason="Supplier stock",
        )
        for user in (people.tech_mad, people.partner):
            page = html(client_for(user).get(url("detail", found)))
            for sentinel in Sentinel:
                assert str(sentinel.value) not in page

    def test_another_requesters_edits_are_not_readable(
        self, make_request: MakeRequest, people: People
    ) -> None:
        found = submitted(make_request, people)
        line = found.lines.get(line_no=1)
        engineer = client_for(people.engineer)
        save(
            engineer,
            dialog(engineer, url("edit_line", line)),
            requested_qty="12",
            reason="Secret reason",
        )
        response = client_for(people.partner).get(url("detail", found))
        assert response.status_code == 404

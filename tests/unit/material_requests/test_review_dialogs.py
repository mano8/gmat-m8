"""The review page's action dialogs (M8b.4): returning, assigning, closing, reopening and
cancelling a request, and reducing, closing, reopening and removing a line, each an inline
dialog of the page.

The page draws one dialog per offered action (ids unique per line), a destructive one states
what it will do and has Cancel focused. Sent with htmx a form that cannot be sent comes back
inside its open dialog (422); one that is saved navigates (204 + ``HX-Redirect``); a plain POST
keeps the flash and the redirect.
"""

import re
from typing import Any

import pytest
from django.test import Client

from apps.material_requests.models import MaterialRequest, MaterialRequestLine
from tests.org import People
from tests.unit.material_requests.conftest import MakeRequest
from tests.unit.material_requests.test_review_workflow_views import (
    approved,
    client_for,
    html,
    messages_of,
    submitted,
    url,
)

pytestmark = pytest.mark.django_db

HTMX = {"HX-Request": "true"}
Status = MaterialRequest.Status


def post(client: Client, target: str, data: dict[str, str] | None = None, **extra: Any) -> Any:
    return client.post(target, data or {}, **extra)


def ids_of(page: str) -> list[str]:
    return re.findall(r'\sid="([^"]+)"', page)


class TestThePageDrawsTheDialogs:
    def test_a_submitted_request_offers_return_and_send_with_the_consequences(
        self, make_request: MakeRequest, people: People
    ) -> None:
        found = submitted(make_request, people)
        page = html(client_for(people.engineer).get(url("examine", found)))
        assert 'id="return-dialog"' in page and 'id="assign-dialog"' in page
        assert 'data-testid="return-form"' in page and 'data-testid="consequences"' in page
        # A destructive dialog opens with Cancel focused; the page has no <details> holding a form.
        assert re.search(r"autofocus[^>]*data-testid=\"dialog-cancel\"", page)
        assert "<details" not in page.split("<main", 1)[1].split("</main>", 1)[0]

    def test_an_approved_request_offers_close_and_cancel_to_the_material_manager(
        self, make_request: MakeRequest, people: People
    ) -> None:
        found = approved(make_request, people)
        page = html(client_for(people.mm_mad).get(url("examine", found)))
        assert 'id="close-dialog"' in page
        assert "Every line still open is closed short." in page

    def test_a_closed_request_offers_reopen(
        self, make_request: MakeRequest, people: People
    ) -> None:
        found = approved(make_request, people)
        client = client_for(people.mm_mad)
        post(client, url("close", found), {"reason": "Not needed"})
        found.refresh_from_db()
        assert found.status == Status.CLOSED
        page = html(client.get(url("examine", found)))
        assert 'id="reopen-dialog"' in page and 'id="close-dialog"' not in page

    def test_every_line_has_dialogs_and_no_id_repeats(
        self, make_request: MakeRequest, people: People
    ) -> None:
        found = approved(make_request, people, lines=3)
        page = html(client_for(people.mm_mad).get(url("examine", found)))
        for number in (1, 2, 3):
            assert f'id="reduce-dialog-{number}"' in page
            assert f'id="close-line-dialog-{number}"' in page
        ids = ids_of(page)
        repeated = sorted({i for i in ids if ids.count(i) > 1})
        assert not repeated, repeated

    def test_a_requester_without_review_sees_none_of_them(
        self, make_request: MakeRequest, people: People
    ) -> None:
        found = submitted(make_request, people)
        response = client_for(people.tech_mad).get(url("examine", found))
        assert response.status_code == 403


class TestSentFromTheDialog:
    def test_an_empty_return_comes_back_in_the_dialog(
        self, make_request: MakeRequest, people: People
    ) -> None:
        found = submitted(make_request, people)
        response = post(client_for(people.engineer), url("return_to_draft", found), headers=HTMX)
        assert response.status_code == 422
        assert 'data-testid="return-form"' in html(response)
        assert "data-error-summary" in html(response)
        assert "Vary" in response and "HX-Request" in response["Vary"]
        found.refresh_from_db()
        assert found.status == Status.SUBMITTED

    def test_a_return_that_is_saved_navigates(
        self, make_request: MakeRequest, people: People
    ) -> None:
        found = submitted(make_request, people)
        response = post(
            client_for(people.engineer),
            url("return_to_draft", found),
            {"note": "Quantities are off"},
            headers=HTMX,
        )
        assert response.status_code == 204
        assert response["HX-Redirect"].endswith("/requests/inbox/")
        found.refresh_from_db()
        assert found.status == Status.DRAFT

    def test_a_plain_post_still_flashes_and_redirects(
        self, make_request: MakeRequest, people: People
    ) -> None:
        found = submitted(make_request, people)
        client = client_for(people.engineer)
        response = post(client, url("return_to_draft", found), {"note": "No"}, follow=True)
        assert any("returned" in message for message in messages_of(response))

    def test_closing_a_line_without_a_reason_keeps_the_line_dialog_ids(
        self, make_request: MakeRequest, people: People
    ) -> None:
        found = approved(make_request, people)
        line = found.lines.get(line_no=2)
        response = post(client_for(people.mm_mad), url("close_line", line), headers=HTMX)
        assert response.status_code == 422
        page = html(response)
        assert 'id="id_reason_close_2"' in page
        assert "Give a reason." in page
        line.refresh_from_db()
        assert line.closed_short_at is None

    def test_a_line_is_closed_short_from_its_dialog(
        self, make_request: MakeRequest, people: People
    ) -> None:
        found = approved(make_request, people)
        line = found.lines.get(line_no=1)
        response = post(
            client_for(people.mm_mad),
            url("close_line", line),
            {"reason": "Cancelled"},
            headers=HTMX,
        )
        assert response.status_code == 204
        line.refresh_from_db()
        assert line.closed_short_at is not None

    def test_a_reduction_below_the_floor_is_refused_in_the_dialog(
        self, make_request: MakeRequest, people: People
    ) -> None:
        found = approved(make_request, people)
        line = found.lines.get(line_no=1)
        response = post(
            client_for(people.engineer),
            url("reduce_line", line),
            {"approved_qty": "-1"},
            headers=HTMX,
        )
        assert response.status_code == 422
        assert 'data-testid="reduce-form"' in html(response)
        line.refresh_from_db()
        assert line.approved_qty == line.requested_qty

    def test_a_reduction_is_saved(self, make_request: MakeRequest, people: People) -> None:
        found = approved(make_request, people)
        line = found.lines.get(line_no=1)
        response = post(
            client_for(people.engineer),
            url("reduce_line", line),
            {"approved_qty": "1", "reason": "Smaller phase"},
            headers=HTMX,
        )
        assert response.status_code == 204
        line.refresh_from_db()
        assert line.approved_qty == 1

    def test_a_service_refusal_shows_on_the_form(
        self, make_request: MakeRequest, people: People
    ) -> None:
        found = approved(make_request, people)
        # Reopening a line that was never closed: the service refuses.
        line = found.lines.get(line_no=1)
        response = post(
            client_for(people.mm_mad), url("reopen_line", line), {"reason": "Oops"}, headers=HTMX
        )
        assert response.status_code == 422
        assert 'data-testid="reopen-line-form"' in html(response)

    def test_cancelling_and_reopening_the_request(
        self, make_request: MakeRequest, people: People
    ) -> None:
        found = submitted(make_request, people)
        client = client_for(people.engineer)
        response = post(client, url("cancel_any", found), {"reason": "Duplicate"}, headers=HTMX)
        assert response.status_code == 204
        found.refresh_from_db()
        assert found.status == Status.CANCELLED
        response = post(client, url("reopen", found), headers=HTMX)
        assert response.status_code == 422

    def test_removing_a_line_without_a_reason_comes_back_with_its_ids(
        self, make_request: MakeRequest, people: People
    ) -> None:
        found = submitted(make_request, people)
        line: MaterialRequestLine = found.lines.get(line_no=2)
        response = post(
            client_for(people.engineer), url("remove_reviewed_line", line), headers=HTMX
        )
        assert response.status_code == 422
        assert 'id="id_reason_remove_2"' in html(response)

    def test_assigning_without_a_note_comes_back_in_the_dialog(
        self, make_request: MakeRequest, people: People
    ) -> None:
        found = submitted(make_request, people)
        response = post(
            client_for(people.engineer),
            url("assign_reviewer", found),
            {"reviewer": str(people.mm_mad.pk)},
            headers=HTMX,
        )
        assert response.status_code == 422
        assert 'data-testid="assign-form"' in html(response)


class TestTheRequestersDraft:
    def test_the_draft_page_confirms_a_discard_in_a_dialog_and_shows_the_steps(
        self, make_request: MakeRequest, people: People
    ) -> None:
        draft = make_request()
        page = html(client_for(people.tech_mad).get(url("detail", draft)))
        assert 'id="discard-draft-dialog"' in page and 'data-testid="stepper"' in page
        assert "Nothing has been sent to a reviewer yet." in page
        assert 'aria-current="step"' in page

    def test_discarding_from_the_dialog_navigates_to_the_list(
        self, make_request: MakeRequest, people: People
    ) -> None:
        draft = make_request()
        response = post(client_for(people.tech_mad), url("cancel", draft), headers=HTMX)
        assert response.status_code == 204
        assert response["HX-Redirect"].endswith("/requests/")
        draft.refresh_from_db()
        assert draft.status == Status.CANCELLED

    def test_a_plain_post_still_flashes_and_redirects(
        self, make_request: MakeRequest, people: People
    ) -> None:
        draft = make_request()
        response = post(client_for(people.tech_mad), url("cancel", draft), follow=True)
        assert any("discarded" in message for message in messages_of(response))

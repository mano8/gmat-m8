"""The lazy dialog pilot of M8b.3: a reviewer changes a request line in a dialog.

The review page's "Change the line" is a link to the change page that htmx loads into the
page's one dialog; the change page answers its ``line_dialog`` fragment to htmx and the whole
page otherwise. The form posts with htmx: 204 + ``HX-Redirect`` when saved, the form again
with its errors (422) when it can be corrected (the same key may be sent again: it produced
nothing), 409 when the request changed meanwhile. A key bound to a result never makes a
second change.
"""

from decimal import Decimal
from typing import Any

import pytest
from django.test import Client

from apps.catalog.models import Material
from apps.material_requests import services
from apps.material_requests.models import MaterialRequest, MaterialRequestLineRevision
from tests.org import People
from tests.support import principal_for
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
# What htmx sends from the review page's dialog (the content it swaps).
IN_DIALOG = {**HTMX, "HX-Target": "line-dialog-content"}


def fragment(client: Client, target: str, **params: str) -> Any:
    response = client.get(target, params, headers=IN_DIALOG)
    assert response.status_code == 200, response.status_code
    return response


def payload(response: Any, **data: str) -> dict[str, str]:
    """The dialog's form as it was shown (key and page version), with ``data``."""
    form = response.context["form"]
    return {
        "idempotency_key": form["idempotency_key"].value(),
        "seen_version": str(form["seen_version"].value()),
        **data,
    }


def send(client: Client, response: Any, **data: str) -> Any:
    return client.post(response.context["action"], payload(response, **data), headers=IN_DIALOG)


def revisions() -> int:
    return MaterialRequestLineRevision.objects.count()


def first_line(found: MaterialRequest) -> Any:
    return found.lines.get(line_no=1)


class TestTheReviewPage:
    def test_the_link_opens_the_change_page_in_the_one_dialog(
        self, make_request: MakeRequest, people: People
    ) -> None:
        found = submitted(make_request, people)
        page = html(client_for(people.engineer).get(url("examine", found)))
        edit = url("edit_line", first_line(found))
        assert f'href="{edit}"' in page and f'hx-get="{edit}"' in page
        assert 'data-dialog-open="line-dialog"' in page
        assert page.count('<dialog id="line-dialog"') == 1
        assert 'hx-target="#line-dialog-content"' in page

    def test_no_dialog_for_who_cannot_edit(self, make_request: MakeRequest, people: People) -> None:
        found = submitted(make_request, people)
        page = html(client_for(people.tech_mad).get(url("detail", found)))
        assert '<dialog id="line-dialog"' not in page


class TestTheDialogPage:
    def test_htmx_gets_only_the_fragment_with_the_dialogs_cancel(
        self, make_request: MakeRequest, people: People
    ) -> None:
        line = first_line(submitted(make_request, people))
        response = fragment(client_for(people.engineer), url("edit_line", line))
        body = html(response)
        assert "<html" not in body and 'data-testid="line-form"' in body
        assert f'hx-post="{url("apply_edit", line)}"' in body
        assert 'commandfor="line-dialog"' in body and 'command="close"' in body
        assert "HX-Request" in response["Vary"]
        assert "no-store" in response["Cache-Control"]

    def test_without_htmx_the_whole_page_with_a_way_back(
        self, make_request: MakeRequest, people: People
    ) -> None:
        line = first_line(submitted(make_request, people))
        response = client_for(people.engineer).get(url("edit_line", line))
        body = html(response)
        assert "<html" in body and "data-dialog-content" in body
        assert 'commandfor="line-dialog"' not in body
        assert f'href="{url("examine", line.request)}"' in body
        assert "HX-Request" in response["Vary"]

    def test_the_change_pages_own_swaps_keep_its_way_back(
        self, make_request: MakeRequest, people: People
    ) -> None:
        """The change page opened as a link swaps its search into its own panel: no dialog
        there to close, so Cancel stays the link back."""
        line = first_line(submitted(make_request, people))
        response = client_for(people.engineer).get(url("edit_line", line), headers=HTMX)
        body = html(response)
        assert "<html" not in body and 'commandfor="line-dialog"' not in body
        assert f'href="{url("examine", line.request)}"' in body
        assert "HX-Target" in response["Vary"]

    def test_the_search_and_its_choices_stay_on_the_dialogs_url(
        self, make_request: MakeRequest, people: People, breaker: Material
    ) -> None:
        line = first_line(submitted(make_request, people))
        edit = url("edit_line", line)
        body = html(fragment(client_for(people.engineer), edit, q="Diferencial"))
        assert f'hx-get="{edit}"' in body and f'action="{edit}"' in body
        assert f'hx-get="{edit}?material={breaker.pk}"' in body

    def test_a_line_that_cannot_be_edited_sends_the_whole_page_back(
        self, make_request: MakeRequest, people: People
    ) -> None:
        live = approved(make_request, people)
        line = first_line(live)
        services.close_line_short(principal_for(people.mm_mad), line.pk, reason="Gone")
        client = client_for(people.engineer)
        response = client.get(url("edit_line", line), headers=HTMX)
        assert response.status_code == 204
        assert response["HX-Redirect"] == url("examine", live)
        page = client.get(response["HX-Redirect"])
        assert "This line can no longer be edited." in messages_of(page)


class TestSendingTheDialog:
    def test_saved_the_page_navigates_and_shows_the_flash(
        self, make_request: MakeRequest, people: People
    ) -> None:
        found, client = submitted(make_request, people), client_for(people.engineer)
        line = first_line(found)
        response = send(
            client, fragment(client, url("edit_line", line)), requested_qty="9", reason="Fewer"
        )
        assert response.status_code == 204
        assert response["HX-Redirect"] == url("examine", found)
        assert "Location" not in response
        assert "Line 1 changed." in messages_of(client.get(response["HX-Redirect"]))
        line.refresh_from_db()
        assert line.requested_qty == Decimal("9")

    def test_invalid_comes_back_in_the_dialog_and_the_same_key_then_saves(
        self, make_request: MakeRequest, people: People
    ) -> None:
        found, client = submitted(make_request, people), client_for(people.engineer)
        line = first_line(found)
        shown = fragment(client, url("edit_line", line))
        refused = send(client, shown, requested_qty="9")  # no reason
        assert refused.status_code == 422
        body = html(refused)
        assert "<html" not in body and "data-error-summary" in body and "Give a reason." in body
        assert 'commandfor="line-dialog"' in body  # still the dialog's Cancel
        assert revisions() == 0
        # The form shown again carries the same key: it produced nothing, so it may be sent.
        assert payload(refused)["idempotency_key"] == payload(shown)["idempotency_key"]
        saved = send(client, refused, requested_qty="9", reason="Fewer")
        assert saved.status_code == 204 and revisions() == 1

    def test_a_service_refusal_comes_back_on_the_form_and_a_correction_saves(
        self, make_request: MakeRequest, people: People
    ) -> None:
        found, client = submitted(make_request, people), client_for(people.engineer)
        line = first_line(found)
        shown = fragment(client, url("edit_line", line))
        refused = send(client, shown, reason="Nothing")  # changes nothing
        assert refused.status_code == 422
        assert "Change something before saving." in html(refused)
        assert revisions() == 0
        saved = send(client, refused, requested_qty="7", reason="Seven")
        assert saved.status_code == 204 and revisions() == 1

    def test_a_replay_leads_to_the_recorded_result_and_changes_nothing_more(
        self, make_request: MakeRequest, people: People
    ) -> None:
        found, client = submitted(make_request, people), client_for(people.engineer)
        line = first_line(found)
        shown = fragment(client, url("edit_line", line))
        assert send(client, shown, requested_qty="11", reason="Twice").status_code == 204
        replay = send(client, shown, requested_qty="11", reason="Twice")
        assert replay.status_code == 204 and replay["HX-Redirect"] == url("examine", found)
        # The same key with other data never makes a second change.
        other = send(client, shown, requested_qty="3", reason="Other")
        assert other.status_code == 204 and other["HX-Redirect"] == url("examine", found)
        assert revisions() == 1
        line.refresh_from_db()
        assert line.requested_qty == Decimal("11")

    def test_a_request_changed_meanwhile_is_409_in_the_dialog(
        self, make_request: MakeRequest, people: People
    ) -> None:
        found, client = submitted(make_request, people), client_for(people.engineer)
        line = first_line(found)
        shown = fragment(client, url("edit_line", line))
        second = found.lines.get(line_no=2)
        other = client_for(people.mm_mad)
        assert (
            send(
                other, fragment(other, url("edit_line", second)), requested_qty="2", reason="First"
            ).status_code
            == 204
        )
        stale = send(client, shown, requested_qty="9", reason="Late")
        assert stale.status_code == 409
        body = html(stale)
        assert 'data-testid="dialog-problem"' in body and "Reload the page" in body
        assert "<html" not in body and revisions() == 1

    def test_the_plain_post_keeps_its_flash_and_redirect(
        self, make_request: MakeRequest, people: People
    ) -> None:
        found, client = submitted(make_request, people), client_for(people.engineer)
        line = first_line(found)
        shown = fragment(client, url("edit_line", line))
        response = client.post(url("apply_edit", line), payload(shown, requested_qty="9"))
        assert response.status_code == 302
        assert response["Location"].startswith(url("edit_line", line))


class TestRefusedAccess:
    @pytest.mark.parametrize(
        ("who", "status"),
        [("tech_mad", 403), ("mm_sev", 404)],
        ids=["no-capability", "out-of-scope"],
    )
    def test_refused_like_the_plain_request_and_never_a_fragment(
        self, make_request: MakeRequest, people: People, who: str, status: int
    ) -> None:
        line = first_line(submitted(make_request, people))
        client = client_for(getattr(people, who))
        shown = client.get(url("edit_line", line), headers=HTMX)
        sent = client.post(url("apply_edit", line), {"reason": "x"}, headers=HTMX)
        assert shown.status_code == sent.status_code == status
        assert 'data-testid="line-form"' not in html(shown)
        assert revisions() == 0

    def test_a_signed_out_browser_is_sent_to_sign_in(
        self, make_request: MakeRequest, people: People
    ) -> None:
        line = first_line(submitted(make_request, people))
        response = Client().post(url("apply_edit", line), {"reason": "x"}, headers=HTMX)
        assert response.status_code == 302 and "/accounts/login/" in response["Location"]

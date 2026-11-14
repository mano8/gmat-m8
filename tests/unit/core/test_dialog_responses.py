"""The htmx form contract of a dialog (M8b.3): every branch of ``apps.core.responses``.

A plain POST keeps the screens' answers (a flash and a redirect); an htmx POST from a dialog
gets 204 + ``HX-Redirect`` on success, the form again with its errors (422) when it can be
corrected, and the dialog's problem fragment (409, 400) when it cannot. Each answer says
``Vary: HX-Request``. The pilots' views are tested in their apps; access refusals (403, 404,
the redirect to sign in) are the views' and are tested there and in the acceptance matrix.
"""

from typing import Any
from uuid import UUID

import pytest
from django import forms
from django.contrib.auth.models import AnonymousUser
from django.contrib.messages import get_messages
from django.contrib.messages.storage.cookie import CookieStorage
from django.core.exceptions import ValidationError
from django.http import HttpRequest, HttpResponse
from django.test import RequestFactory
from django.utils import translation
from django_htmx.middleware import HtmxDetails

from apps.core import responses
from apps.core.idempotency import (
    IdempotencyConflict,
    IdempotencyKeyExpired,
    IdempotencyKeyInvalid,
)
from apps.core.locking import RetryExhausted
from apps.core.responses import Dialog, go, invalid, perform

HTMX = {"HTTP_HX_REQUEST": "true"}
SUMMARY = "components/error_summary.html"


class ReasonForm(forms.Form):
    reason = forms.CharField(label="Reason", error_messages={"required": "Give a reason."})


def make_request(*, htmx: bool = False, restore: bool = False) -> HttpRequest:
    headers: dict[str, str] = dict(HTMX) if htmx else {}
    if restore:
        headers["HTTP_HX_HISTORY_RESTORE_REQUEST"] = "true"
    request = RequestFactory().post("/somewhere/", **headers)  # type: ignore[arg-type]
    request.htmx = HtmxDetails(request)  # type: ignore[attr-defined]
    request.user = AnonymousUser()
    request._messages = CookieStorage(request)  # type: ignore[attr-defined]
    return request


def flashed(request: HttpRequest) -> list[str]:
    return [str(message) for message in get_messages(request)]


def dialog_for(form: forms.Form) -> Dialog:
    return Dialog(form, SUMMARY, lambda bound: {"form": bound})


def body(response: HttpResponse) -> str:
    return response.content.decode()


def varies(response: HttpResponse) -> bool:
    return "HX-Request" in response.get("Vary", "")


def refuse(exc: Exception) -> Any:
    def call() -> None:
        raise exc

    return call


@pytest.fixture(autouse=True)
def _english() -> Any:
    with translation.override("en"):
        yield


class TestGo:
    def test_a_plain_post_is_redirected(self) -> None:
        response = go(make_request(), "/there/")
        assert response.status_code == 302 and response["Location"] == "/there/"
        assert varies(response)

    def test_an_htmx_post_gets_204_and_hx_redirect_never_a_redirect(self) -> None:
        response = go(make_request(htmx=True), "/there/")
        assert response.status_code == 204
        assert response["HX-Redirect"] == "/there/"
        assert "Location" not in response and varies(response)

    def test_a_url_name_with_its_arguments(self) -> None:
        response = go(make_request(htmx=True), "receiving:detail", receipt_id=UUID(int=1))
        assert response["HX-Redirect"] == f"/receiving/{UUID(int=1)}/"

    def test_a_history_restore_request_is_answered_like_a_plain_one(self) -> None:
        assert go(make_request(htmx=True, restore=True), "/there/").status_code == 302


class TestInvalid:
    def test_a_plain_post_flashes_the_errors_and_goes_back(self) -> None:
        request = make_request()
        form = ReasonForm({})
        assert not form.is_valid()
        response = invalid(request, form, "/back/", dialog=dialog_for(form))
        assert response.status_code == 302 and response["Location"] == "/back/"
        assert flashed(request) == ["Give a reason."]

    def test_an_htmx_post_from_a_dialog_gets_the_form_again_with_422(self) -> None:
        request = make_request(htmx=True)
        form = ReasonForm({})
        assert not form.is_valid()
        response = invalid(request, form, "/back/", dialog=dialog_for(form))
        assert response.status_code == 422 and varies(response)
        html = body(response)
        assert "data-error-summary" in html and "Give a reason." in html
        assert 'href="#id_reason"' in html
        assert "<html" not in html  # a fragment, never a page
        assert flashed(request) == []

    def test_an_htmx_post_without_a_dialog_navigates_with_the_flash(self) -> None:
        request = make_request(htmx=True)
        form = ReasonForm({})
        assert not form.is_valid()
        response = invalid(request, form, "/back/")
        assert response.status_code == 204 and response["HX-Redirect"] == "/back/"
        assert flashed(request) == ["Give a reason."]

    def test_a_form_without_error_text_still_says_something(self) -> None:
        request = make_request()
        form = ReasonForm({"reason": "x"})
        assert form.is_valid()
        invalid(request, form, "/back/")
        assert flashed(request) == ["Check the form."]


class TestPerform:
    def test_success_plain(self) -> None:
        request = make_request()
        response = perform(request, lambda: None, "Done.", "/to/")
        assert response.status_code == 302 and response["Location"] == "/to/"
        assert flashed(request) == ["Done."]

    def test_success_htmx(self) -> None:
        request = make_request(htmx=True)
        form = ReasonForm({"reason": "x"})
        response = perform(request, lambda: None, "Done.", "/to/", dialog=dialog_for(form))
        assert response.status_code == 204 and response["HX-Redirect"] == "/to/"
        assert flashed(request) == ["Done."]  # shown on the page htmx navigates to

    def test_the_flash_and_the_place_may_follow_the_result(self) -> None:
        """M10.2: a create goes to the page of what it made."""
        request = make_request(htmx=True)
        response = perform(
            request, lambda: "e7", lambda made: f"Made {made}.", lambda made: f"/made/{made}/"
        )
        assert response.status_code == 204 and response["HX-Redirect"] == "/made/e7/"
        assert flashed(request) == ["Made e7."]

    def test_a_place_that_follows_the_result_needs_failed_to(self) -> None:
        request = make_request()
        with pytest.raises(TypeError, match="failed_to"):
            perform(request, refuse(ValidationError("No.")), "Done.", lambda made: "/x/")
        response = perform(
            request, refuse(ValidationError("No.")), "Done.", lambda made: "/x/", failed_to="/f/"
        )
        assert response["Location"] == "/f/"

    def test_a_refusal_plain_is_flashed_and_goes_to_failed_to(self) -> None:
        request = make_request()
        form = ReasonForm({"reason": "x"})
        call = refuse(ValidationError("Not now."))
        response = perform(
            request, call, "Done.", "/to/", failed_to="/form/", dialog=dialog_for(form)
        )
        assert response.status_code == 302 and response["Location"] == "/form/"
        assert flashed(request) == ["Not now."]

    def test_a_refusal_htmx_comes_back_on_the_form(self) -> None:
        request = make_request(htmx=True)
        form = ReasonForm({"reason": "x"})
        assert form.is_valid()
        call = refuse(ValidationError({"reason": "Too short."}))
        response = perform(request, call, "Done.", "/to/", dialog=dialog_for(form))
        assert response.status_code == 422
        assert "Too short." in body(response) and 'href="#id_reason"' in body(response)
        assert flashed(request) == []

    def test_a_refusal_without_a_field_is_on_top_of_the_form(self) -> None:
        request = make_request(htmx=True)
        form = ReasonForm({"reason": "x"})
        assert form.is_valid()
        response = perform(
            request, refuse(ValidationError("Not now.")), "", "/to/", dialog=dialog_for(form)
        )
        assert response.status_code == 422 and "Not now." in body(response)

    @pytest.mark.parametrize(
        "exc",
        [
            ValidationError("This request was changed after you opened it.", code="edited"),
            ValidationError({"reason": ValidationError("Changed.", code="edited")}),
        ],
        ids=["error-list", "error-dict"],
    )
    def test_a_page_out_of_date_htmx_is_409_with_the_way_out(self, exc: ValidationError) -> None:
        request = make_request(htmx=True)
        form = ReasonForm({"reason": "x"})
        assert form.is_valid()
        response = perform(request, refuse(exc), "", "/to/", dialog=dialog_for(form))
        assert response.status_code == 409 and varies(response)
        html = body(response)
        assert 'data-testid="dialog-problem"' in html and "Reload the page" in html
        assert "<html" not in html

    def test_a_page_out_of_date_plain_is_flashed_as_before(self) -> None:
        request = make_request()
        form = ReasonForm({"reason": "x"})
        call = refuse(ValidationError("Changed after you opened it.", code="edited"))
        response = perform(request, call, "", "/to/", dialog=dialog_for(form))
        assert response.status_code == 302
        assert flashed(request) == ["Changed after you opened it."]

    def test_a_refusal_htmx_without_a_dialog_navigates_with_the_flash(self) -> None:
        request = make_request(htmx=True)
        response = perform(request, refuse(ValidationError("Not now.")), "", "/to/")
        assert response.status_code == 204 and response["HX-Redirect"] == "/to/"
        assert flashed(request) == ["Not now."]

    @pytest.mark.parametrize(
        ("exc", "status"),
        [
            (IdempotencyConflict("other data"), 409),
            (IdempotencyKeyExpired("expired"), 409),
            (IdempotencyKeyInvalid("tampered"), 400),
            (RetryExhausted("busy"), 409),
        ],
        ids=["conflict", "expired", "invalid-key", "retry-exhausted"],
    )
    def test_no_correction_helps_htmx_gets_the_problem_fragment(
        self, exc: Exception, status: int
    ) -> None:
        request = make_request(htmx=True)
        form = ReasonForm({"reason": "x"})
        response = perform(request, refuse(exc), "", "/to/", dialog=dialog_for(form))
        assert response.status_code == status and varies(response)
        html = body(response)
        assert 'data-testid="dialog-problem"' in html and "<html" not in html
        assert ("out of date" in html) is (status == 409)

    @pytest.mark.parametrize(
        ("exc", "status"),
        [(IdempotencyConflict("other data"), 409), (IdempotencyKeyInvalid("tampered"), 400)],
        ids=["conflict", "invalid-key"],
    )
    def test_no_correction_helps_plain_gets_the_error_page(
        self, exc: Exception, status: int
    ) -> None:
        response = perform(make_request(), refuse(exc), "", "/to/")
        assert response.status_code == status
        assert "<html" in body(response) and 'data-testid="dialog-problem"' not in body(response)


class TestProblemTemplate:
    def test_the_conflict_answer_is_the_same_helper_for_every_view(self) -> None:
        response = responses.conflict(make_request(htmx=True))
        assert response.status_code == 409 and "Reload the page" in body(response)
        assert "data-error-summary" in body(response)  # app.js focuses it

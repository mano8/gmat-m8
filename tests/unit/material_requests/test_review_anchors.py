"""The review page keeps its place (M14.2, Q80): a decision opens the page at the next pending
line, every other per-line action returns to its own line, and *approve all* can be asked from
the head of the page, in a dialog.

The anchor is the line's own card (``id="line-<n>"``); the number comes from the database.
"""

from decimal import Decimal
from typing import Any

import pytest
from django.test import Client

from apps.access.context import AccessContext
from apps.material_requests import selectors, services
from apps.material_requests.models import MaterialRequest, MaterialRequestLine
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

Status = MaterialRequest.Status
Decision = MaterialRequestLine.Decision
HTMX = {"HX-Request": "true"}


def line_of(found: MaterialRequest, line_no: int) -> MaterialRequestLine:
    return found.lines.get(line_no=line_no)


def decide(client: Client, line: MaterialRequestLine, **data: str) -> Any:
    """Post the line's decision as its form sends it (a plain POST: no ``follow``)."""
    sent = {
        "decision": "APPROVED",
        "approved_qty": "5",
        "seen_version": str(selectors.version(line.request)),
        **data,
    }
    return client.post(url("decide_line", line), sent)


def location(response: Any) -> str:
    return str(response["Location"])


def examine(found: MaterialRequest, line_no: int | None = None) -> str:
    base = url("examine", found)
    return base if line_no is None else f"{base}#line-{line_no}"


class TestNextPending:
    def test_the_next_line_after_the_decided_one(
        self, make_request: MakeRequest, people: People
    ) -> None:
        found = submitted(make_request, people, lines=3)
        response = decide(client_for(people.mm_mad), line_of(found, 1))
        assert response.status_code == 302 and location(response) == examine(found, 2)

    def test_a_middle_line_moves_on_to_the_one_after_it(
        self, make_request: MakeRequest, people: People
    ) -> None:
        found = submitted(make_request, people, lines=3)
        client = client_for(people.mm_mad)
        assert location(decide(client, line_of(found, 1))) == examine(found, 2)
        assert location(decide(client, line_of(found, 2))) == examine(found, 3)

    def test_after_the_last_line_it_returns_to_the_first_one_still_pending(
        self, make_request: MakeRequest, people: People
    ) -> None:
        found = submitted(make_request, people, lines=3)
        client = client_for(people.mm_mad)
        decide(client, line_of(found, 2))
        assert location(decide(client, line_of(found, 3))) == examine(found, 1)

    def test_a_rejection_moves_on_too(self, make_request: MakeRequest, people: People) -> None:
        found = submitted(make_request, people, lines=3)
        response = decide(
            client_for(people.mm_mad), line_of(found, 2), decision="REJECTED", note="Not needed"
        )
        assert location(response) == examine(found, 3)
        assert line_of(found, 2).decision == Decision.REJECTED

    def test_the_only_line_leaves_no_anchor(
        self, make_request: MakeRequest, people: People
    ) -> None:
        found = submitted(make_request, people, lines=1)
        response = decide(client_for(people.mm_mad), line_of(found, 1))
        assert location(response) == examine(found)
        found.refresh_from_db()
        assert found.status == Status.APPROVED

    def test_the_last_pending_line_leaves_no_anchor(
        self, make_request: MakeRequest, people: People
    ) -> None:
        found = submitted(make_request, people, lines=2)
        client = client_for(people.mm_mad)
        decide(client, line_of(found, 2))
        assert location(decide(client, line_of(found, 1))) == examine(found)

    def test_a_decision_taken_again_still_moves_on(
        self, make_request: MakeRequest, people: People
    ) -> None:
        """The reviewer who decided a line may change it: the next one waiting is the answer."""
        found = submitted(make_request, people, lines=3)
        client = client_for(people.mm_mad)
        decide(client, line_of(found, 1))
        assert location(decide(client, line_of(found, 1), approved_qty="4")) == examine(found, 2)

    def test_the_page_offers_every_line_as_a_target(
        self, make_request: MakeRequest, people: People
    ) -> None:
        found = submitted(make_request, people, lines=3)
        page = html(client_for(people.mm_mad).get(url("examine", found)))
        for line_no in (1, 2, 3):
            assert f'id="line-{line_no}"' in page
        assert page.count('tabindex="-1"') >= 3 and "scroll-mt-20" in page


class TestRefusals:
    def test_a_refused_decision_returns_to_its_line(
        self, make_request: MakeRequest, people: People
    ) -> None:
        found = submitted(make_request, people, lines=3)
        client = client_for(people.mm_mad)
        response = decide(client, line_of(found, 2), approved_qty="11")
        assert location(response) == examine(found, 2)
        assert any(
            "At most the requested quantity" in m
            for m in messages_of(client.get(location(response)))
        )
        assert line_of(found, 2).decision == Decision.PENDING

    def test_an_invalid_form_returns_to_its_line(
        self, make_request: MakeRequest, people: People
    ) -> None:
        found = submitted(make_request, people, lines=3)
        response = decide(
            client_for(people.mm_mad), line_of(found, 3), decision="REJECTED", note=""
        )
        assert location(response) == examine(found, 3)

    def test_a_stale_page_returns_to_its_line(
        self, make_request: MakeRequest, people: People
    ) -> None:
        found = submitted(make_request, people, lines=3)
        line = line_of(found, 2)
        version = str(selectors.version(found))
        services.edit_line(
            principal_for(people.engineer),
            line_of(found, 1).pk,
            services.LineEdit("More", {"requested_qty": Decimal("12")}),
        )
        response = decide(client_for(people.mm_mad), line, seen_version=version)
        assert location(response) == examine(found, 2)
        assert line_of(found, 2).decision == Decision.PENDING


class TestOtherLineActions:
    def test_sourcing_returns_to_its_line(self, make_request: MakeRequest, people: People) -> None:
        found = approved(make_request, people, lines=2)
        line = line_of(found, 2)
        response = client_for(people.mm_mad).post(url("set_sourcing", line), {"sourcing": "STOCK"})
        assert response.status_code == 302 and location(response) == examine(found, 2)

    def test_an_invalid_sourcing_returns_to_its_line(
        self, make_request: MakeRequest, people: People
    ) -> None:
        found = approved(make_request, people, lines=2)
        response = client_for(people.mm_mad).post(
            url("set_sourcing", line_of(found, 2)), {"sourcing": "NOPE"}
        )
        assert location(response) == examine(found, 2)

    def test_the_catalog_link_returns_to_its_line(
        self, make_request: MakeRequest, people: People
    ) -> None:
        found = approved(make_request, people, lines=2)
        line = line_of(found, 2)
        # A catalog line has nothing to link: it is sent back to where it came from.
        response = client_for(people.mm_mad).get(url("link", line))
        assert response.status_code == 302 and location(response) == examine(found, 2)

    def test_the_dialogs_of_a_line_answer_htmx_with_its_anchor(
        self, make_request: MakeRequest, people: People
    ) -> None:
        found = approved(make_request, people, lines=2)
        line = line_of(found, 2)
        client = client_for(people.mm_mad)
        reduced = client.post(
            url("reduce_line", line),
            {"approved_qty": "4", "reason": "Less"},
            headers=HTMX,
        )
        assert reduced.status_code == 204 and reduced["HX-Redirect"] == examine(found, 2)
        assert reduced["HX-Refresh"] == "true"  # the fragment alone would not reload the page
        closed = client.post(url("close_line", line), {"reason": "Gone"}, headers=HTMX)
        assert closed.status_code == 204 and closed["HX-Redirect"] == examine(found, 2)
        reopened = client.post(url("reopen_line", line), {"reason": "Back"}, headers=HTMX)
        assert reopened.status_code == 204 and reopened["HX-Redirect"] == examine(found, 2)

    def test_the_requests_own_actions_stay_on_the_page(
        self, make_request: MakeRequest, people: People
    ) -> None:
        found = approved(make_request, people, lines=2)
        response = client_for(people.mm_mad).post(
            url("schedule", found), {"priority": "HIGH", "needed_by": ""}
        )
        assert location(response) == examine(found)


class TestApproveAllFromTheTop:
    def test_the_head_offers_the_dialog_while_lines_wait(
        self, make_request: MakeRequest, people: People
    ) -> None:
        found = submitted(make_request, people, lines=3)
        page = html(client_for(people.mm_mad).get(url("examine", found)))
        assert 'data-testid="approve-all-open"' in page
        assert (
            'data-testid="approve-all-dialog"' in page and 'data-testid="approve-all-form"' in page
        )
        assert "3 lines wait for a decision: each is approved at its requested quantity." in page
        assert f'action="{url("approve_all", found)}"' in page
        # The bottom bar's button stays direct, and the two forms never share a field id.
        assert 'data-testid="approve-all"' in page
        assert 'id="id_approve_all_seen_version"' in page and 'id="id_seen_version"' in page

    def test_one_line_is_said_in_the_singular(
        self, make_request: MakeRequest, people: People
    ) -> None:
        found = submitted(make_request, people, lines=1)
        page = html(client_for(people.mm_mad).get(url("examine", found)))
        assert "1 line waits for a decision: it is approved at its requested quantity." in page

    def test_the_dialog_counts_only_what_still_waits(
        self, make_request: MakeRequest, people: People
    ) -> None:
        found = submitted(make_request, people, lines=3)
        client = client_for(people.mm_mad)
        decide(client, line_of(found, 1))
        page = html(client.get(url("examine", found)))
        assert "2 lines wait for a decision" in page

    def test_no_dialog_once_nothing_waits_or_for_an_approved_request(
        self, make_request: MakeRequest, people: People
    ) -> None:
        found = approved(make_request, people, lines=2)
        page = html(client_for(people.mm_mad).get(url("examine", found)))
        assert 'data-testid="approve-all-open"' not in page
        assert 'data-testid="approve-all-dialog"' not in page

    def test_no_dialog_to_who_cannot_decide(
        self, make_request: MakeRequest, people: People
    ) -> None:
        own = make_request(Status.SUBMITTED, user=people.site_manager)
        page = html(client_for(people.site_manager).get(url("examine", own)))
        assert 'data-testid="approve-all-open"' not in page
        assert 'data-testid="approve-all-dialog"' not in page

    @pytest.mark.parametrize("who", ["tech_mad", "wo_mad", "partner"])
    def test_who_does_not_review_reaches_neither_page_nor_action(
        self, make_request: MakeRequest, people: People, who: str
    ) -> None:
        found = submitted(make_request, people, lines=2)
        client = client_for(getattr(people, who))
        assert client.get(url("examine", found)).status_code in {403, 404}
        denied = client.post(url("approve_all", found), {"seen_version": "1"}, headers=HTMX)
        assert denied.status_code in {403, 404}
        assert not found.lines.exclude(decision=Decision.PENDING).exists()

    def test_confirming_approves_every_line_and_the_page_navigates(
        self, make_request: MakeRequest, people: People
    ) -> None:
        found = submitted(make_request, people, lines=3)
        client = client_for(people.mm_mad)
        response = client.post(
            url("approve_all", found),
            {"seen_version": str(selectors.version(found))},
            headers=HTMX,
        )
        assert response.status_code == 204 and response["HX-Redirect"] == examine(found)
        assert "HX-Request" in response["Vary"]
        found.refresh_from_db()
        assert found.status == Status.APPROVED
        assert "Every pending line is approved." in messages_of(client.get(examine(found)))

    def test_a_stale_page_gets_the_problem_in_the_dialog(
        self, make_request: MakeRequest, people: People
    ) -> None:
        found = submitted(make_request, people, lines=2)
        version = str(selectors.version(found))
        services.edit_line(
            principal_for(people.engineer),
            line_of(found, 1).pk,
            services.LineEdit("More", {"requested_qty": Decimal("12")}),
        )
        response = client_for(people.mm_mad).post(
            url("approve_all", found), {"seen_version": version}, headers=HTMX
        )
        assert response.status_code == 409
        found.refresh_from_db()
        assert found.status == Status.SUBMITTED

    def test_a_form_without_the_version_comes_back_in_the_dialog(
        self, make_request: MakeRequest, people: People
    ) -> None:
        found = submitted(make_request, people, lines=2)
        response = client_for(people.mm_mad).post(url("approve_all", found), {}, headers=HTMX)
        assert response.status_code == 422
        body = html(response)
        assert 'data-testid="approve-all-form"' in body
        assert "2 lines wait for a decision" in body

    def test_the_bottom_bars_button_still_posts_plainly(
        self, make_request: MakeRequest, people: People
    ) -> None:
        found = submitted(make_request, people, lines=2)
        response = client_for(people.mm_mad).post(
            url("approve_all", found), {"seen_version": str(selectors.version(found))}
        )
        assert response.status_code == 302 and location(response) == examine(found)


class TestSelectors:
    def ctx(self, people: People) -> AccessContext:
        return AccessContext.for_user(people.mm_mad)

    def settle(
        self, found: MaterialRequest, people: People, numbers: list[int], decision: str
    ) -> None:
        note = "Not needed" if decision == "REJECTED" else ""
        for number in numbers:
            services.decide_line(
                principal_for(people.mm_mad),
                line_of(found, number).pk,
                decision=decision,
                note=note,
            )

    def test_next_pending_wraps_and_ends(self, make_request: MakeRequest, people: People) -> None:
        found = submitted(make_request, people, lines=4)
        ctx = self.ctx(people)
        assert selectors.next_pending(ctx, found, 1) == 2
        assert selectors.next_pending(ctx, found, 4) == 1
        self.settle(found, people, [2, 3], "APPROVED")
        assert selectors.next_pending(ctx, found, 1) == 4
        assert selectors.next_pending(ctx, found, 4) == 1
        self.settle(found, people, [1], "REJECTED")
        assert selectors.next_pending(ctx, found, 1) == 4
        self.settle(found, people, [4], "APPROVED")
        assert selectors.next_pending(ctx, found, 4) is None
        assert selectors.pending_count(ctx, found) == 0

    def test_pending_count(self, make_request: MakeRequest, people: People) -> None:
        found = submitted(make_request, people, lines=3)
        ctx = self.ctx(people)
        assert selectors.pending_count(ctx, found) == 3
        self.settle(found, people, [1], "REJECTED")
        assert selectors.pending_count(ctx, found) == 2

    def test_nothing_outside_the_users_scope_is_counted(
        self, make_request: MakeRequest, people: People
    ) -> None:
        found = submitted(make_request, people, lines=3)
        outsider = AccessContext.for_user(people.partner)
        assert selectors.pending_count(outsider, found) == 0
        assert selectors.next_pending(outsider, found, 1) is None

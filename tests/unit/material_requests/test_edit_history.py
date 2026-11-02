"""Reads of the reviewer edits (M7b.3, Q34): each line against its as-submitted copy, the
edits since, the version a page sends back, and the timeline labels.

What the requester's and the review pages will show (M7b.4): only fields a line already
shows, and nothing for who may not view the request.
"""

from decimal import Decimal
from typing import Any
from uuid import UUID

import pytest

from apps.access.context import AccessContext
from apps.access.roles import Capability
from apps.accounts.models import User
from apps.catalog.models import Material
from apps.material_requests import selectors, services
from apps.material_requests.ledger import Flows
from apps.material_requests.models import (
    MaterialRequest,
    MaterialRequestBaseline,
    MaterialRequestBaselineLine,
    MaterialRequestLineRevision,
)
from apps.material_requests.services import LineEdit
from tests.org import People
from tests.support import principal_for
from tests.unit.material_requests.conftest import MakeRequest

pytestmark = pytest.mark.django_db

Status = MaterialRequest.Status
Kind = MaterialRequestLineRevision.Kind
D = Decimal


def ctx(user: User) -> AccessContext:
    return AccessContext.for_user(user)


@pytest.fixture
def approved(make_request: MakeRequest, people: People) -> MaterialRequest:
    tech, mm = principal_for(people.tech_mad), principal_for(people.mm_mad)
    request = services.submit(tech, make_request(lines=3).pk)
    return services.approve_all(mm, request.pk)


def by_line(history: list[selectors.LineHistory]) -> dict[int, Any]:
    return {entry.line.line_no: entry for entry in history}


def test_each_line_against_its_copy_with_the_edits_since(
    approved: MaterialRequest,
    people: People,
    breaker: Material,
    flows_of: dict[UUID, Flows],
) -> None:
    engineer = principal_for(people.engineer)
    first, second, third = approved.lines.order_by("line_no")
    services.edit_line(engineer, first.pk, LineEdit("More", {"requested_qty": D(12)}))
    services.edit_line(engineer, first.pk, LineEdit("Less", {"requested_qty": D(11)}))
    flows_of[second.pk] = Flows(delivered=D(1))
    replaced = services.edit_line(
        engineer, second.pk, LineEdit("Swap", {"material_id": breaker.pk, "requested_qty": D(9)})
    )
    services.remove_line_as_reviewer(engineer, third.pk, reason="Not needed")
    added = services.add_line_as_reviewer(
        engineer, approved.pk, LineEdit("Also", {"material_id": breaker.pk, "requested_qty": D(2)})
    )

    history = by_line(selectors.line_history(ctx(people.tech_mad), approved))
    assert sorted(history) == [1, 2, 3, 4, 5]
    assert history[1].submitted.requested_qty == D(10)
    assert history[1].line.requested_qty == D(11)
    assert [edit.reason for edit in history[1].revisions] == ["More", "Less"]
    assert history[1].edited
    assert [edit.pk for edit in history[2].revisions] == [replaced.pk]
    assert history[2].line.closed_short_at is not None  # replaced lines stay visible
    assert history[3].line.closed_short_source == "LINE"  # removed lines stay visible
    assert [edit.kind for edit in history[3].revisions] == [Kind.REMOVE]
    assert history[4].submitted is None  # the replacement
    assert [edit.pk for edit in history[4].revisions] == [replaced.pk]
    assert history[5].submitted is None
    assert [edit.pk for edit in history[5].revisions] == [added.pk]
    assert history[5].revisions[0].editor == people.engineer


def test_only_the_latest_submit_counts(make_request: MakeRequest, people: People) -> None:
    tech, mm = principal_for(people.tech_mad), principal_for(people.mm_mad)
    request = services.submit(tech, make_request().pk)
    line = request.lines.get()
    services.edit_line(mm, line.pk, LineEdit("x", {"requested_qty": D(4)}))
    services.return_to_draft(mm, request.pk, note="Check it")
    services.update_line(tech, line.pk, requested_qty=D(6))
    services.submit(tech, request.pk)
    (entry,) = selectors.line_history(ctx(people.tech_mad), request)
    assert entry.submitted is not None and entry.submitted.requested_qty == D(6)
    assert entry.revisions == () and not entry.edited


def test_a_request_without_a_copy(make_request: MakeRequest, people: People) -> None:
    request = make_request(Status.SUBMITTED)
    (entry,) = selectors.line_history(ctx(people.mm_mad), request)
    assert (entry.submitted, entry.revisions) == (None, ())


@pytest.mark.parametrize("who", ["partner", "mm_sev", "nobody"])
def test_nothing_for_who_may_not_view_it(
    approved: MaterialRequest, people: People, who: str
) -> None:
    assert selectors.line_history(ctx(getattr(people, who)), approved) == []


def test_the_version_a_page_sends_back(approved: MaterialRequest, people: People) -> None:
    assert selectors.version(approved) == 1
    services.edit_line(
        principal_for(people.mm_mad),
        approved.lines.order_by("line_no")[0].pk,
        LineEdit("x", {"requested_qty": D(3)}),
    )
    assert selectors.version(approved) == 2


@pytest.mark.parametrize(
    ("who", "status", "expected"),
    [
        ("mm_mad", Status.APPROVED, True),
        ("engineer", Status.SUBMITTED, True),
        ("group_admin", Status.APPROVED, True),
        ("site_manager", Status.SUBMITTED, False),
        ("tech_mad", Status.APPROVED, False),
        ("mm_sev", Status.APPROVED, False),
        ("mm_mad", Status.CLOSED, False),
        ("mm_mad", Status.DRAFT, False),
    ],
)
def test_who_is_offered_the_edit_tools(
    make_request: MakeRequest, people: People, who: str, status: str, expected: bool
) -> None:
    request = make_request(status, decided=status == Status.CLOSED)
    assert selectors.can_edit_lines(ctx(getattr(people, who)), request) is expected


def test_the_timeline_names_each_edit(
    approved: MaterialRequest, people: People, breaker: Material
) -> None:
    mm = principal_for(people.mm_mad)
    first, second, third = approved.lines.order_by("line_no")
    services.edit_line(mm, first.pk, LineEdit("More cable", {"requested_qty": D(12)}))
    services.edit_line(mm, second.pk, LineEdit("Swap", {"material_id": breaker.pk}))
    services.remove_line_as_reviewer(mm, third.pk, reason="Gone")
    services.add_line_as_reviewer(
        mm, approved.pk, LineEdit("Extra", {"material_id": breaker.pk, "requested_qty": D(1)})
    )
    entries = selectors.history(ctx(people.mm_mad), approved)
    edits = [(entry.action, entry.detail) for entry in entries][-4:]
    assert edits == [
        ("request_line.edited", "More cable"),
        ("request_line.edited", "Swap"),
        ("request_line.removed_by_reviewer", "Gone"),
        ("request_line.added_by_reviewer", "Extra"),
    ]
    assert all(entry.label != entry.action for entry in entries)


@pytest.mark.parametrize(
    "model", [MaterialRequestBaseline, MaterialRequestBaselineLine, MaterialRequestLineRevision]
)
def test_history_rows_are_scoped_like_their_request(
    approved: MaterialRequest, people: People, model: Any
) -> None:
    services.edit_line(
        principal_for(people.mm_mad),
        approved.lines.order_by("line_no")[0].pk,
        LineEdit("x", {"requested_qty": D(3)}),
    )
    for who in ("tech_mad", "mm_mad", "engineer"):
        rows = model.objects.visible_to(ctx(getattr(people, who)), Capability.REQUEST_VIEW)
        assert rows.exists()
    for who in ("partner", "mm_sev"):
        rows = model.objects.visible_to(ctx(getattr(people, who)), Capability.REQUEST_VIEW)
        assert not rows.exists()
    assert str(approved.number) in str(model.objects.first())

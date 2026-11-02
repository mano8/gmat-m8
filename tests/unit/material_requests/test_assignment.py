"""Reviewer assignment (M7b.2, Q31, Q35, ADR-0014 §4): services and selectors.

The assignee holds ``request.review`` through a group administrator's, material manager's
or engineer's membership whose scope covers the procurement branch (S6, S8, S9). The
requester picks one at submit; any reviewer in scope reassigns later, with a note. The
assignment never restricts who decides, and a stored assignee who no longer qualifies
reads as unassigned without anything being rewritten.
"""

from collections.abc import Callable, Iterator
from datetime import timedelta
from typing import Any

import pytest
from django.core.exceptions import ValidationError
from django.utils import timezone

from apps.access.context import AccessContext
from apps.access.exceptions import AccessDenied, MFARequired, NotInScope
from apps.access.roles import Role
from apps.accounts.models import User
from apps.audit.models import AuditEvent
from apps.core import events
from apps.core.idempotency import IdempotencyConflict, issue_key
from apps.core.ids import new_id
from apps.material_requests import selectors, services
from apps.material_requests.models import MaterialRequest
from apps.organizations.models import Branch, Company, Membership
from apps.projects.models import Project
from tests.org import Org, People, also
from tests.support import error_codes, principal_for
from tests.unit.material_requests.conftest import MakeRequest

pytestmark = pytest.mark.django_db

Status = MaterialRequest.Status
HOUR = timedelta(hours=1)


@pytest.fixture
def assigned_events() -> Iterator[list[events.Event]]:
    seen: list[events.Event] = []
    unsubscribe = events.subscribe("request.reviewer_assigned", seen.append)
    yield seen
    unsubscribe()


def fresh(request: MaterialRequest) -> MaterialRequest:
    return MaterialRequest.objects.get(pk=request.pk)


def assign(by: User, request: MaterialRequest, to: User, note: str = "Over to you") -> Any:
    return services.assign_reviewer(principal_for(by), request.pk, reviewer_id=to.pk, note=note)


def submitted_to(people: People, make_request: MakeRequest, reviewer: User) -> MaterialRequest:
    """A hospital draft of A-MAD's technician, submitted to ``reviewer``."""
    draft = make_request()
    return services.submit(principal_for(people.tech_mad), draft.pk, reviewer_id=reviewer.pk)


# --- who qualifies --------------------------------------------------------------------


def expired_mm(org: Org, member: Callable[..., User]) -> User:
    now = timezone.now()
    return member(
        Role.MATERIAL_MANAGER,
        email="mm-expired@example.org",
        branch=org.a_mad,
        valid_from=now - 48 * HOUR,
        valid_until=now - HOUR,
    )


def deactivated_mm(org: Org, member: Callable[..., User]) -> User:
    user = member(Role.MATERIAL_MANAGER, email="mm-gone@example.org", branch=org.a_mad)
    User.objects.filter(pk=user.pk).update(is_active=False)
    return user


def b_mad_mm(org: Org, member: Callable[..., User]) -> User:
    """A material manager of B-MAD: the managed branch does not reach A-MAD (S1)."""
    return member(Role.MATERIAL_MANAGER, email="mm-bmad@example.org", branch=org.b_mad)


def sevilla_admin_elsewhere(org: Org, member: Callable[..., User]) -> User:
    """A technician of A-MAD who is a material manager of A-SEV (S6)."""
    user = member(Role.TECHNICIAN, email="tech-sev-mm@example.org", branch=org.a_mad)
    also(user, Role.MATERIAL_MANAGER, branch=org.a_sev)
    return user


INELIGIBLE: dict[str, Callable[[Org, People, Callable[..., User]], User]] = {
    "site manager": lambda org, people, member: people.site_manager,  # reviews, wrong role
    "technician": lambda org, people, member: people.tech_mad,
    "warehouse operator": lambda org, people, member: people.wo_mad,
    "partner": lambda org, people, member: people.partner,
    "other branch": lambda org, people, member: people.mm_sev,
    "role held elsewhere": lambda org, people, member: people.mixed,
    "managed branch": lambda org, people, member: b_mad_mm(org, member),
    "revoked": lambda org, people, member: people.revoked,
    "expired": lambda org, people, member: expired_mm(org, member),
    "deactivated": lambda org, people, member: deactivated_mm(org, member),
    "platform operator": lambda org, people, member: people.operator,
    "no membership": lambda org, people, member: people.nobody,
}


# --- at submit ------------------------------------------------------------------------


class TestSubmitToReviewer:
    @pytest.mark.parametrize("reviewer", ["mm_mad", "engineer", "group_admin"])
    def test_the_requester_picks_a_reviewer(
        self, make_request: MakeRequest, people: People, reviewer: str
    ) -> None:
        chosen = getattr(people, reviewer)
        request = submitted_to(people, make_request, chosen)
        stored = fresh(request)
        assert stored.status == Status.SUBMITTED
        assert stored.assigned_reviewer_id == chosen.pk
        assert stored.assigned_by_id == people.tech_mad.pk
        assert stored.assigned_at is not None
        event = AuditEvent.objects.get(action="request.submitted")
        assert event.data["changes"]["assigned_reviewer_id"] == {
            "from": None,
            "to": str(chosen.pk),
        }

    def test_without_a_pick_the_request_is_unassigned(
        self, make_request: MakeRequest, people: People
    ) -> None:
        request = services.submit(principal_for(people.tech_mad), make_request().pk)
        stored = fresh(request)
        assert (stored.assigned_reviewer_id, stored.assigned_at, stored.assigned_by_id) == (
            None,
            None,
            None,
        )
        assert "changes" not in AuditEvent.objects.get(action="request.submitted").data

    @pytest.mark.parametrize("case", sorted(INELIGIBLE))
    def test_an_ineligible_reviewer_is_refused_and_nothing_is_submitted(
        self,
        make_request: MakeRequest,
        org: Org,
        people: People,
        member: Callable[..., User],
        case: str,
    ) -> None:
        reviewer = INELIGIBLE[case](org, people, member)
        draft = make_request()
        with pytest.raises(ValidationError) as exc:
            services.submit(principal_for(people.tech_mad), draft.pk, reviewer_id=reviewer.pk)
        assert error_codes(exc) == {"ineligible_reviewer"}
        stored = fresh(draft)
        assert (stored.status, stored.number, stored.assigned_reviewer_id) == (
            Status.DRAFT,
            None,
            None,
        )

    def test_an_unknown_user_is_refused_like_an_ineligible_one(
        self, make_request: MakeRequest, people: People
    ) -> None:
        with pytest.raises(ValidationError) as exc:
            services.submit(principal_for(people.tech_mad), make_request().pk, reviewer_id=new_id())
        assert error_codes(exc) == {"ineligible_reviewer"}

    def test_judged_against_the_procurement_branch_the_submit_takes(
        self, make_request: MakeRequest, org: Org, people: People, member: Callable[..., User]
    ) -> None:
        """The hospital's buyer changed to A-SEV after the draft started (S5 snapshot)."""
        tech = member(Role.TECHNICIAN, email="tech-bmad@example.org", branch=org.b_mad)
        draft = make_request(user=tech)
        Project.objects.filter(pk=org.hospital.pk).update(procurement_branch=org.a_sev)
        with pytest.raises(ValidationError):
            services.submit(principal_for(tech), draft.pk, reviewer_id=people.mm_mad.pk)
        submitted = services.submit(principal_for(tech), draft.pk, reviewer_id=people.mm_sev.pk)
        assert submitted.assigned_reviewer_id == people.mm_sev.pk

    def test_an_engineer_may_send_their_own_request_to_themselves(
        self, make_request: MakeRequest, people: People
    ) -> None:
        draft = make_request(user=people.engineer)
        submitted = services.submit(
            principal_for(people.engineer), draft.pk, reviewer_id=people.engineer.pk
        )
        assert submitted.assigned_reviewer_id == people.engineer.pk

    def test_a_resubmit_keeps_the_assignee_unless_another_is_picked(
        self, make_request: MakeRequest, people: People
    ) -> None:
        tech = principal_for(people.tech_mad)
        request = submitted_to(people, make_request, people.mm_mad)
        services.withdraw(tech, request.pk)
        assert services.submit(tech, request.pk).assigned_reviewer_id == people.mm_mad.pk
        services.withdraw(tech, request.pk)
        again = services.submit(tech, request.pk, reviewer_id=people.engineer.pk)
        assert again.assigned_reviewer_id == people.engineer.pk

    def test_the_pick_is_part_of_the_idempotent_payload(
        self, make_request: MakeRequest, people: People
    ) -> None:
        tech, draft = principal_for(people.tech_mad), make_request()
        key = issue_key(people.tech_mad.pk, "request.submit")
        first = services.submit(tech, draft.pk, idempotency_key=key, reviewer_id=people.mm_mad.pk)
        again = services.submit(tech, draft.pk, idempotency_key=key, reviewer_id=people.mm_mad.pk)
        assert again.assigned_reviewer_id == first.assigned_reviewer_id == people.mm_mad.pk
        with pytest.raises(IdempotencyConflict):
            services.submit(tech, draft.pk, idempotency_key=key, reviewer_id=people.engineer.pk)


# --- reassignment ---------------------------------------------------------------------


class TestAssignReviewer:
    @pytest.mark.parametrize("by", ["mm_mad", "engineer", "group_admin", "site_manager"])
    def test_any_reviewer_in_scope_reassigns_with_an_audited_note(
        self,
        make_request: MakeRequest,
        people: People,
        by: str,
        assigned_events: list[events.Event],
    ) -> None:
        request = submitted_to(people, make_request, people.mm_mad)
        actor = getattr(people, by)
        assigned = assign(actor, request, people.engineer, note="  Engineer's call  ")
        stored = fresh(assigned)
        assert stored.assigned_reviewer_id == people.engineer.pk
        assert stored.assigned_by_id == actor.pk
        event = AuditEvent.objects.get(action="request.reviewer_assigned")
        assert event.actor_id == actor.pk
        assert event.data["note"] == "Engineer's call"
        assert event.data["changes"]["assigned_reviewer_id"] == {
            "from": str(people.mm_mad.pk),
            "to": str(people.engineer.pk),
        }
        (published,) = assigned_events
        assert published.payload == {
            "request_id": request.pk,
            "reviewer_id": people.engineer.pk,
            "assigned_by": actor.pk,
            "note": "Engineer's call",
        }

    def test_an_unassigned_request_gets_its_first_assignee(
        self, make_request: MakeRequest, people: People
    ) -> None:
        request = make_request(Status.SUBMITTED)
        assign(people.mm_mad, request, people.mm_mad)
        assert fresh(request).assigned_reviewer_id == people.mm_mad.pk

    def test_an_approved_request_can_be_reassigned(
        self, make_request: MakeRequest, people: People
    ) -> None:
        request = make_request(Status.APPROVED, decided=True)
        assign(people.mm_mad, request, people.engineer)
        assert fresh(request).assigned_reviewer_id == people.engineer.pk

    @pytest.mark.parametrize(
        "status", [Status.DRAFT, Status.REJECTED, Status.CANCELLED, Status.CLOSED]
    )
    def test_other_states_refuse(
        self, make_request: MakeRequest, people: People, status: str
    ) -> None:
        request = make_request(status)
        with pytest.raises(ValidationError) as exc:
            assign(people.mm_mad, request, people.engineer)
        assert error_codes(exc) == {"transition"}
        assert fresh(request).assigned_reviewer_id is None

    def test_the_note_is_required(self, make_request: MakeRequest, people: People) -> None:
        request = make_request(Status.SUBMITTED)
        with pytest.raises(ValidationError) as exc:
            assign(people.mm_mad, request, people.engineer, note="   ")
        assert error_codes(exc) == {"required"}

    def test_the_current_assignee_again_changes_nothing(
        self,
        make_request: MakeRequest,
        people: People,
        assigned_events: list[events.Event],
    ) -> None:
        request = make_request(Status.SUBMITTED)
        assign(people.mm_mad, request, people.engineer)
        before = fresh(request).assigned_at
        assign(people.group_admin, request, people.engineer)
        stored = fresh(request)
        assert (stored.assigned_at, stored.assigned_by_id) == (before, people.mm_mad.pk)
        assert AuditEvent.objects.filter(action="request.reviewer_assigned").count() == 1
        assert len(assigned_events) == 1

    @pytest.mark.parametrize("case", sorted(INELIGIBLE))
    def test_an_ineligible_assignee_is_refused(
        self,
        make_request: MakeRequest,
        org: Org,
        people: People,
        member: Callable[..., User],
        case: str,
    ) -> None:
        request = submitted_to(people, make_request, people.mm_mad)
        reviewer = INELIGIBLE[case](org, people, member)
        with pytest.raises(ValidationError) as exc:
            assign(people.group_admin, request, reviewer)
        assert error_codes(exc) == {"ineligible_reviewer"}
        assert fresh(request).assigned_reviewer_id == people.mm_mad.pk
        assert not AuditEvent.objects.filter(action="request.reviewer_assigned").exists()

    @pytest.mark.parametrize("deactivate", ["branch", "company"])
    def test_an_assignee_whose_organization_is_inactive_is_refused(
        self, make_request: MakeRequest, org: Org, people: People, deactivate: str
    ) -> None:
        """A-MAD (or company A) closed after the submit: its memberships grant nothing."""
        request = make_request(Status.SUBMITTED)
        if deactivate == "branch":
            Branch.objects.filter(pk=org.a_mad.pk).update(is_active=False)
        else:
            Company.objects.filter(pk=org.a.pk).update(is_active=False)
        with pytest.raises(ValidationError) as exc:
            assign(people.group_admin, request, people.mm_mad)
        assert error_codes(exc) == {"ineligible_reviewer"}

    @pytest.mark.parametrize(
        ("by", "error"),
        [
            ("tech_mad", AccessDenied),  # the requester reviews nothing
            ("wo_mad", AccessDenied),
            ("mm_sev", NotInScope),
            ("partner", NotInScope),
            ("revoked", NotInScope),
            ("mixed", AccessDenied),  # sees their own A-MAD requests only, reviews A-SEV
            ("nobody", NotInScope),
        ],
    )
    def test_who_may_not_reassign(
        self,
        make_request: MakeRequest,
        people: People,
        by: str,
        error: type[Exception],
    ) -> None:
        request = make_request(Status.SUBMITTED, user=people.mixed if by == "mixed" else None)
        with pytest.raises(error):
            assign(getattr(people, by), request, people.engineer)
        assert fresh(request).assigned_reviewer_id is None

    def test_an_engineer_without_session_mfa_is_refused(
        self, make_request: MakeRequest, people: People
    ) -> None:
        request = make_request(Status.SUBMITTED)
        with pytest.raises(MFARequired):
            services.assign_reviewer(
                principal_for(people.engineer, mfa_at=None),
                request.pk,
                reviewer_id=people.engineer.pk,
                note="Mine",
            )

    def test_the_assignment_never_restricts_who_decides(
        self, make_request: MakeRequest, people: People
    ) -> None:
        request = submitted_to(people, make_request, people.engineer)
        approved = services.approve_all(principal_for(people.mm_mad), request.pk)
        assert approved.status == Status.APPROVED


# --- selectors ------------------------------------------------------------------------


def ctx(user: User) -> AccessContext:
    return AccessContext.for_user(user)


class TestEligibleReviewers:
    def test_the_requester_of_a_draft_gets_the_reviewers_of_its_branch(
        self, make_request: MakeRequest, org: Org, people: People, member: Callable[..., User]
    ) -> None:
        late = member(Role.ENGINEER, email="a-engineer@example.org", branch=org.a_mad)
        draft = make_request()
        offered = selectors.eligible_reviewers(ctx(people.tech_mad), draft)
        assert set(offered) == {people.mm_mad, people.engineer, people.group_admin, late}
        assert offered[0] == late  # by name: no full names here, so by email

    def test_a_reviewer_may_reassign_a_submitted_request(
        self, make_request: MakeRequest, people: People
    ) -> None:
        request = make_request(Status.SUBMITTED)
        assert people.engineer in selectors.eligible_reviewers(ctx(people.site_manager), request)

    @pytest.mark.parametrize(
        ("who", "status"),
        [
            ("tech_mad", Status.SUBMITTED),  # the requester once it is submitted
            ("mm_mad", Status.DRAFT),  # a reviewer before the submit
            ("mm_mad", Status.CLOSED),
            ("mm_sev", Status.SUBMITTED),  # out of scope
        ],
    )
    def test_nobody_else_gets_choices(
        self, make_request: MakeRequest, people: People, who: str, status: str
    ) -> None:
        request = make_request(status)
        assert selectors.eligible_reviewers(ctx(getattr(people, who)), request) == []

    def test_reviewers_of_an_inactive_branch_are_not_offered(
        self, make_request: MakeRequest, org: Org, people: People, member: Callable[..., User]
    ) -> None:
        """A-MAD buys for SEV-2 and is closed (S9): only group administrators reach it (S2)."""
        tech = member(Role.TECHNICIAN, email="tech-sev@example.org", branch=org.a_sev)
        draft = make_request(user=tech, project=org.sev_for_mad)
        Branch.objects.filter(pk=org.a_mad.pk).update(is_active=False)
        assert selectors.eligible_reviewers(ctx(tech), draft) == [people.group_admin]


class TestAssignee:
    def test_a_revoked_assignee_reads_as_unassigned_and_nothing_is_rewritten(
        self, make_request: MakeRequest, people: People
    ) -> None:
        request = submitted_to(people, make_request, people.mm_mad)
        assert selectors.assignee(fresh(request)) == people.mm_mad
        assert list(selectors.assigned_to(ctx(people.mm_mad))) == [request]
        Membership.objects.filter(user=people.mm_mad).update(revoked_at=timezone.now())
        assert selectors.assignee(fresh(request)) is None
        assert list(selectors.assigned_to(ctx(people.mm_mad))) == []
        assert fresh(request).assigned_reviewer_id == people.mm_mad.pk

    def test_an_expired_assignee_reads_as_unassigned(
        self, make_request: MakeRequest, people: People
    ) -> None:
        request = submitted_to(people, make_request, people.engineer)
        Membership.objects.filter(user=people.engineer).update(
            valid_until=timezone.now() - timedelta(seconds=1)
        )
        assert selectors.assignee(fresh(request)) is None
        assert list(selectors.assigned_to(ctx(people.engineer))) == []

    def test_unassigned(self, make_request: MakeRequest) -> None:
        assert selectors.assignee(make_request(Status.SUBMITTED)) is None


class TestAssignedTo:
    def test_submitted_and_approved_requests_assigned_to_the_user(
        self, make_request: MakeRequest, people: People
    ) -> None:
        by, mine = people.mm_mad, {}
        for status in (Status.SUBMITTED, Status.APPROVED, Status.CLOSED, Status.REJECTED):
            mine[status] = make_request(
                status,
                assigned_reviewer=people.engineer,
                assigned_at=timezone.now(),
                assigned_by=by,
            )
        make_request(  # someone else's
            Status.SUBMITTED, assigned_reviewer=by, assigned_at=timezone.now(), assigned_by=by
        )
        make_request(Status.SUBMITTED)  # unassigned
        found = set(selectors.assigned_to(ctx(people.engineer)))
        assert found == {mine[Status.SUBMITTED], mine[Status.APPROVED]}

    def test_the_most_pressing_first(self, make_request: MakeRequest, people: People) -> None:
        stamp = {
            "assigned_reviewer": people.mm_mad,
            "assigned_at": timezone.now(),
            "assigned_by": people.mm_mad,
        }
        normal = make_request(Status.SUBMITTED, **stamp)
        urgent = make_request(Status.SUBMITTED, priority="URGENT", **stamp)
        rows = list(selectors.assigned_to(ctx(people.mm_mad)))
        assert rows == [urgent, normal]
        assert [vars(row)["line_count"] for row in rows] == [1, 1]

    def test_an_assignee_whose_branch_no_longer_buys_for_the_request(
        self, make_request: MakeRequest, org: Org, people: People
    ) -> None:
        """A request moved to A-SEV's buying (a fixture write) leaves A-MAD's list."""
        request = submitted_to(people, make_request, people.mm_mad)
        MaterialRequest.objects.filter(pk=request.pk).update(procurement_branch=org.a_sev)
        assert list(selectors.assigned_to(ctx(people.mm_mad))) == []


class TestCanAssign:
    @pytest.mark.parametrize(
        ("who", "status", "expected"),
        [
            ("mm_mad", Status.SUBMITTED, True),
            ("site_manager", Status.APPROVED, True),
            ("mm_mad", Status.DRAFT, False),
            ("mm_mad", Status.CANCELLED, False),
            ("tech_mad", Status.SUBMITTED, False),
            ("mm_sev", Status.SUBMITTED, False),
        ],
    )
    def test_states_and_capability(
        self, make_request: MakeRequest, people: People, who: str, status: str, expected: bool
    ) -> None:
        request = make_request(status)
        assert selectors.can_assign(ctx(getattr(people, who)), request) is expected

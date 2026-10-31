"""The selectors of the M2.6 screens: who reads what (plan §3.4 to §3.6)."""

import pytest

from apps.access.context import AccessContext
from apps.access.exceptions import NotInScope
from apps.accounts import selectors as accounts
from apps.accounts.models import Invitation
from apps.accounts.tokens import hash_token, new_token
from apps.organizations import selectors as organizations
from apps.organizations.models import Membership
from apps.projects import selectors as projects
from apps.projects.models import ProjectAssignment
from tests.org import Org, People

pytestmark = pytest.mark.django_db


def ctx(user: object) -> AccessContext:
    return AccessContext.for_user(user)  # type: ignore[arg-type]


class TestAdministratorsOnly:
    """Accounts, memberships and tokens are read with ``org.manage`` alone (S2)."""

    def test_others_read_nothing(self, org: Org, people: People) -> None:
        manager = ctx(people.mm_mad)
        target = people.tech_mad
        assert not accounts.people(manager).exists()
        assert not accounts.assignments_of(manager, target).exists()
        assert not accounts.invitations(manager).exists()
        assert not accounts.email_changes(manager, target).exists()
        assert not organizations.companies(manager).exists()
        assert not organizations.memberships(manager, target).exists()
        membership = Membership.objects.get(user=target)
        for read, pk in (
            (accounts.person, target.pk),
            (accounts.invitation, target.pk),
            (accounts.email_change, target.pk),
            (organizations.membership, membership.pk),
        ):
            with pytest.raises(NotInScope):
                read(manager, pk)

    def test_administrators_read_everything(self, org: Org, people: People) -> None:
        admin = ctx(people.group_admin)
        assert accounts.person(admin, people.operator.pk) == people.operator
        assert set(organizations.companies(admin)) >= {org.a, org.b, org.c}
        assert organizations.membership(admin, Membership.objects.get(user=people.tech_mad).pk)
        assignments = accounts.assignments_of(admin, people.site_manager)
        assert [item.project for item in assignments] == [org.hospital]


class TestTokenLookups:
    def test_by_hash(self, people: People) -> None:
        raw, digest = new_token()
        invitation = Invitation.objects.create(
            email="x@example.org",
            kind=Invitation.Kind.NEW_ACCOUNT,
            invited_by=people.group_admin,
            token_hash=digest,
            expires_at="2099-01-01T00:00Z",
        )
        assert accounts.invitation_by_hash(hash_token(raw)) == invitation
        assert accounts.invitation_by_hash(hash_token("other")) is None
        assert accounts.invitation_by_hash(None) is None
        assert accounts.email_change_by_hash("") is None
        assert accounts.email_change_by_hash(hash_token("other")) is None


class TestProjects:
    def test_unknown_projects_and_assignments_are_not_found(self, org: Org, people: People) -> None:
        partner = ctx(people.partner)
        with pytest.raises(NotInScope):
            projects.project(partner, org.sev_works.pk)
        foreign = ProjectAssignment.objects.create(
            project=org.sev_works,
            user=people.mm_sev,
            kind="MANAGER",
            valid_from="2026-01-01T00:00Z",
        )
        with pytest.raises(NotInScope):
            projects.assignment(partner, foreign.pk)
        with pytest.raises(NotInScope):
            projects.assignment(partner, org.sev_works.pk)  # not an assignment id

    def test_assignments_are_for_managers(self, org: Org, people: People) -> None:
        assert not projects.assignments(ctx(people.partner), org.hospital).exists()
        assert projects.assignments(ctx(people.mm_mad), org.hospital).exists()

    def test_candidates(self, org: Org, people: People) -> None:
        manager = ctx(people.mm_mad)
        assert set(projects.assignment_candidates(manager, "MANAGER")) >= {people.site_manager}
        assert people.partner not in set(projects.assignment_candidates(manager, "MANAGER"))
        assert people.partner in set(projects.assignment_candidates(manager, "PARTNER"))
        # Partners of an inactive company are no candidates.
        assert people.inactive_partner not in set(projects.assignment_candidates(manager))
        assert not projects.assignment_candidates(ctx(people.tech_mad)).exists()
        # Site managers outside the manager's branches are not offered.
        assert people.site_manager not in set(projects.assignment_candidates(ctx(people.mm_sev)))

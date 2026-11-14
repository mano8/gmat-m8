"""The preview of a scope-defining change (M10.2, inventory §3.6) against the A/B/C fixture.

Each change is previewed, then made with the ORM (the triggers run), and the people the preview
names are compared with what ``AccessContext`` resolves before and after it: an independent
reading of the same rules (S1, S4, S8, S9), so the dialog never says something the access
policy does not do.
"""

from collections.abc import Callable
from datetime import timedelta
from uuid import UUID

import pytest
from django.utils import timezone

from apps.access.context import AccessContext
from apps.access.exceptions import NotInScope
from apps.access.roles import ACTIVE_PROJECT_STATUS, Capability, Role
from apps.accounts.models import User
from apps.organizations import selectors
from apps.organizations.models import Branch, Company
from apps.organizations.selectors import BranchMove, ScopePreview
from apps.organizations.selectors import preview as preview_module
from apps.projects.models import Project
from tests.orders import order
from tests.org import Org, People, also, assign, member

pytestmark = pytest.mark.django_db


def reach(user: User) -> frozenset[object]:
    """The branches and projects a user reaches by every capability (``*``: everything),
    the installation-wide catalog left out, as ``AccessContext`` resolves them now."""
    ctx = AccessContext.for_user(User.objects.get(pk=user.pk))
    active = set(Project.objects.filter(status=ACTIVE_PROJECT_STATUS).values_list("pk", flat=True))
    found: set[object] = set()
    for capability in Capability:
        for grant in ctx.scope_for(capability).grants:
            if grant.everything:
                found.add("*")
            found |= grant.branch_ids
            projects = grant.project_ids
            found |= (
                {pk for pk in projects if pk in active} if grant.active_projects_only else projects
            )
    return frozenset(found)


type Reaches = dict[UUID, frozenset[object]]


def emptied(before: Reaches, after: Reaches) -> set[UUID]:
    """Who reached something before and nothing after."""
    return {pk for pk, found in before.items() if found and not after[pk]}


def ids(named: tuple[tuple[UUID, str], ...]) -> set[UUID]:
    return {pk for pk, _name in named}


def check_against_access(
    people: People, preview: ScopePreview, change: Callable[[], object]
) -> None:
    """The preview's people are those whose resolved reach empties (or fills) with ``change``."""
    everyone = list(people.all().values())
    before = {user.pk: reach(user) for user in everyone}
    change()
    after = {user.pk: reach(user) for user in everyone}
    losing, regaining = emptied(before, after), emptied(after, before)
    assert ids(preview.losing) == losing and preview.losing_total == len(losing)
    assert ids(preview.regaining) == regaining and preview.regaining_total == len(regaining)


def admin_ctx(people: People) -> AccessContext:
    return AccessContext.for_user(people.group_admin)


def label(branch: Branch) -> str:
    return f"{branch.company.code}-{branch.code} · {branch.name}"


class TestDeactivatingTheJointVenture:
    """Deactivating B: B-MAD stops granting, A-MAD's people lose it, and the hospital's site
    manager and C's partner, whose only project it is, reach nothing any more."""

    def test_figures(self, org: Org, people: People) -> None:
        preview = selectors.company_activity(admin_ctx(people), org.b, active=False)
        assert preview.stopping == (label(org.b_mad),)
        assert preview.starting == ()
        # The managing branch stays live, so B-MAD leaves its scope rather than "stopping" there.
        assert preview.leaving == (BranchMove(label(org.b_mad), label(org.a_mad)),)
        assert preview.joining == ()
        # A-MAD's material manager, engineer, operator, technician and the mixed user's
        # technician role; the site manager's and the partner's roles on the hospital.
        assert preview.memberships == 7
        assert preview.assignments == 2  # the inactive partner's assigned nothing before
        assert {pk for pk, _name in preview.losing} == {people.site_manager.pk, people.partner.pk}
        assert preview.blocking == () and preview.changes_access

    def test_matches_the_access_policy(self, org: Org, people: People) -> None:
        preview = selectors.company_activity(admin_ctx(people), org.b, active=False)
        check_against_access(
            people, preview, lambda: Company.objects.filter(pk=org.b.pk).update(is_active=False)
        )


class TestBranches:
    def test_deactivating_a_managing_branch(self, org: Org, people: People) -> None:
        preview = selectors.branch_activity(admin_ctx(people), org.a_mad, active=False)
        assert preview.stopping == (label(org.a_mad),)
        assert preview.leaving == ()  # A-MAD is gone itself: B-MAD is not "left" by a live one
        names = {pk for pk, _name in preview.losing}
        assert {people.mm_mad.pk, people.engineer.pk, people.wo_mad.pk, people.tech_mad.pk} <= names
        assert people.mixed.pk not in names  # still a material manager of A-SEV
        check_against_access(
            people,
            preview,
            lambda: Branch.objects.filter(pk=org.a_mad.pk).update(is_active=False),
        )

    def test_reactivating_a_branch(self, org: Org, people: People) -> None:
        old = Branch.objects.get(code="OLD")
        preview = selectors.branch_activity(admin_ctx(people), old, active=True)
        assert preview.starting == (label(old),)
        assert [name for _pk, name in preview.regaining] == ["—"]  # the fixture has no names
        assert {pk for pk, _name in preview.regaining} == {people.inactive_branch.pk}
        assert preview.regaining_more == 0
        assert preview.blocking == ()  # never read for a reactivation
        check_against_access(
            people, preview, lambda: Branch.objects.filter(pk=old.pk).update(is_active=True)
        )

    def test_a_branch_of_an_inactive_company_regains_nothing(
        self, org: Org, people: People
    ) -> None:
        d_hq = Branch.objects.get(company__code="D")
        member(Role.TECHNICIAN, email="tech-d@example.org", branch=d_hq)
        preview = selectors.branch_activity(admin_ctx(people), d_hq, active=True)
        assert not preview.changes_access and preview.regaining == ()

    def test_changing_the_managing_branch(self, org: Org, people: People) -> None:
        preview = selectors.branch_manager(admin_ctx(people), org.b_mad, org.a_sev.pk)
        assert preview.leaving == (BranchMove(label(org.b_mad), label(org.a_mad)),)
        assert preview.joining == (BranchMove(label(org.b_mad), label(org.a_sev)),)
        assert preview.stopping == preview.starting == ()
        assert preview.memberships == 7  # five anchored at A-MAD, two at A-SEV
        assert preview.assignments == 0 and preview.losing == ()
        check_against_access(
            people,
            preview,
            lambda: Branch.objects.filter(pk=org.b_mad.pk).update(managed_by=org.a_sev),
        )

    def test_no_managing_branch_any_more(self, org: Org, people: People) -> None:
        preview = selectors.branch_manager(admin_ctx(people), org.b_mad, None)
        assert preview.leaving == (BranchMove(label(org.b_mad), label(org.a_mad)),)
        assert preview.joining == ()
        check_against_access(
            people,
            preview,
            lambda: Branch.objects.filter(pk=org.b_mad.pk).update(managed_by=None),
        )

    def test_the_same_managing_branch_changes_nothing(self, org: Org, people: People) -> None:
        preview = selectors.branch_manager(admin_ctx(people), org.b_mad, org.a_mad.pk)
        assert not preview.changes_access
        assert preview.memberships == preview.assignments == preview.losing_total == 0

    def test_an_unknown_managing_branch_previews_nothing(self, org: Org, people: People) -> None:
        unknown = UUID(int=7)
        preview = selectors.branch_manager(admin_ctx(people), org.b_mad, unknown)
        assert not preview.changes_access


class TestCompanies:
    def test_reactivating_an_inactive_partner_company(self, org: Org, people: People) -> None:
        d = Company.objects.get(code="D")
        preview = selectors.company_activity(admin_ctx(people), d, active=True)
        assert {pk for pk, _name in preview.regaining} == {people.inactive_partner.pk}
        assert preview.assignments == 1  # the inactive partner's hospital assignment
        check_against_access(
            people, preview, lambda: Company.objects.filter(pk=d.pk).update(is_active=True)
        )

    def test_deactivating_the_buying_company(self, org: Org, people: People) -> None:
        preview = selectors.company_activity(admin_ctx(people), org.a, active=False)
        assert set(preview.stopping) == {label(org.a_hq), label(org.a_mad), label(org.a_sev)}
        check_against_access(
            people, preview, lambda: Company.objects.filter(pk=org.a.pk).update(is_active=False)
        )

    def test_a_partner_on_a_project_on_hold_reaches_nothing_there(
        self, org: Org, people: People
    ) -> None:
        """S4: a partner reaches only active projects, so losing an on-hold one is no loss."""
        partner = member(Role.PARTNER_REQUESTER, email="partner-hold@example.org", company=org.c)
        assign(partner, org.mad_hold, "PARTNER")
        preview = selectors.branch_activity(admin_ctx(people), org.a_mad, active=False)
        assert partner.pk not in {pk for pk, _name in preview.losing}


class TestGroupAdministrators:
    def test_also_a_technician_keeps_everything(self, org: Org, people: People) -> None:
        """A group administrator's role reaches everything whatever the change; one who also
        holds a branch role is read, and loses nothing."""
        both = member(Role.GROUP_ADMIN, email="both@example.org")
        also(both, Role.TECHNICIAN, branch=org.a_mad)
        preview = selectors.branch_activity(admin_ctx(people), org.a_mad, active=False)
        assert both.pk not in {pk for pk, _name in preview.losing}
        check_against_access(
            people, preview, lambda: Branch.objects.filter(pk=org.a_mad.pk).update(is_active=False)
        )


class TestStructureReads:
    """The structure selectors read nothing for who lacks ``org.manage``."""

    def test_nothing_without_org_manage(self, org: Org, people: People) -> None:
        ctx = AccessContext.for_user(people.mm_mad)
        assert selectors.people_count(ctx, org.a) == 0
        assert not selectors.venture_members(ctx, org.b).exists()
        assert selectors.project_counts(ctx, org.a_mad) == {"executes": 0, "buys": 0}
        assert not selectors.company_list(ctx).exists()
        assert not selectors.branch_list(ctx).exists()
        for read, pk in (
            (selectors.company, org.a.pk),
            (selectors.branch, org.a_mad.pk),
            (selectors.venture_member, UUID(int=3)),
        ):
            with pytest.raises(NotInScope):
                read(ctx, pk)

    def test_a_branch_that_manages_others_has_no_candidates(self, org: Org, people: People) -> None:
        assert not selectors.manager_candidates(admin_ctx(people), org.a_mad).exists()
        candidates = selectors.manager_candidates(admin_ctx(people))
        assert org.a_mad in candidates and org.b_mad not in candidates


class TestBlockingDocuments:
    def test_an_issued_order_is_named(self, org: Org, people: People) -> None:
        issued = order(org, status="ISSUED", issued_by=people.mm_mad)
        preview = selectors.branch_activity(admin_ctx(people), org.a_mad, active=False)
        assert [doc.number for doc in preview.blocking] == [issued.number]
        assert preview.blocking_total == 1 and preview.blocking_more == 0
        company = selectors.company_activity(admin_ctx(people), org.a, active=False)
        assert [doc.number for doc in company.blocking] == [issued.number]

    def test_the_list_is_cut(
        self, org: Org, people: People, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(preview_module, "DOCUMENTS_SHOWN", 2)
        for _ in range(3):
            order(org, status="ISSUED", issued_by=people.mm_mad)
        preview = selectors.branch_activity(admin_ctx(people), org.a_mad, active=False)
        assert len(preview.blocking) == 2 and preview.blocking_more == 1


class TestNames:
    def test_the_list_of_people_is_cut_and_ordered(
        self, org: Org, people: People, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(preview_module, "NAMES_SHOWN", 2)
        for name in ("Zoe", "Ana", "Mia"):
            user = member(Role.TECHNICIAN, email=f"{name.lower()}@example.org", branch=org.a_sev)
            User.objects.filter(pk=user.pk).update(full_name=name)
        preview = selectors.branch_activity(admin_ctx(people), org.a_sev, active=False)
        assert [name for _pk, name in preview.losing][:2] == ["Ana", "Mia"]
        assert preview.losing_total >= 3 and preview.losing_more == preview.losing_total - 2

    def test_a_revoked_or_expired_role_counts_for_nothing(self, org: Org, people: People) -> None:
        late = member(
            Role.TECHNICIAN,
            email="late@example.org",
            branch=org.a_sev,
            valid_from=timezone.now() + timedelta(days=1),
        )
        preview = selectors.branch_activity(admin_ctx(people), org.a_sev, active=False)
        assert late.pk not in {pk for pk, _name in preview.losing}


class TestWhoMayRead:
    @pytest.mark.parametrize("who", ["mm_mad", "operator", "tech_mad"])
    def test_only_org_manage(self, org: Org, people: People, who: str) -> None:
        ctx = AccessContext.for_user(getattr(people, who))
        with pytest.raises(NotInScope):
            selectors.branch_activity(ctx, org.a_mad, active=False)
        with pytest.raises(NotInScope):
            selectors.current_generation(ctx)

    def test_the_generation_is_the_access_state(self, org: Org, people: People) -> None:
        before = selectors.company_activity(admin_ctx(people), org.b, active=False).generation
        Company.objects.filter(pk=org.b.pk).update(is_active=False)
        assert selectors.current_generation(admin_ctx(people)) == before + 1

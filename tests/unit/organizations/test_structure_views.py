"""Companies, joint ventures and branches screens (M10.2, inventory §3.1-§3.2).

Every action goes through the screen as a group administrator would send it (a plain POST and
the dialog's htmx POST); the services decide, and their refusals reach the dialog with their
reason. Other actors get 403, unknown objects 404, other methods 405.
"""

from collections.abc import Callable
from decimal import Decimal
from typing import Any

import pytest
from django.test import Client
from django.urls import reverse
from django.utils import timezone, translation
from django.utils.translation import gettext

from apps.access.generations import org_generation
from apps.access.roles import Role
from apps.audit.models import AuditEvent
from apps.core.idempotency import issue_key
from apps.locations.models import Location
from apps.organizations.models import Branch, Company, JointVentureMember, Membership
from tests.orders import order
from tests.org import Org, People, client_for, member
from tests.support import enroll_totp

pytestmark = pytest.mark.django_db

HTMX = {"HX-Request": "true"}
UNKNOWN = "00000000-0000-7000-8000-000000000000"


def in_dialog(dialog: str) -> dict[str, str]:
    return {**HTMX, "HX-Target": f"{dialog}-content"}


def flashes(client: Client) -> list[str]:
    response = client.get(reverse("org:companies"))
    return [str(message) for message in response.context["messages"]]


def generation_in(page: bytes) -> int:
    """The ``seen_generation`` a dialog carries."""
    text = page.decode()
    marker = 'name="seen_generation" value="'
    return int(text[text.index(marker) + len(marker) :].split('"', 1)[0])


def codes(response: Any) -> set[str]:
    """The error codes on the dialog's form (texts are translated; codes are not)."""
    form = response.context["d"].form
    return {error.code for errors in form.errors.as_data().values() for error in errors}


def last_event(action: str) -> AuditEvent:
    return AuditEvent.objects.filter(action=action).order_by("-occurred_at").first()  # type: ignore[return-value]


@pytest.fixture
def admin(people: People) -> Client:
    return client_for(people.group_admin)


def key(people: People, operation: str) -> str:
    return issue_key(people.group_admin.pk, operation)


# --- who may ---------------------------------------------------------------------------------

GETS: list[tuple[str, Callable[[Org], list[object]]]] = [
    ("org:companies", lambda org: []),
    ("org:company", lambda org: [org.b.pk]),
    ("org:new_company", lambda org: []),
    ("org:confirm_deactivate_company", lambda org: [org.b.pk]),
    ("org:confirm_reactivate_company", lambda org: [org.b.pk]),
    ("org:branches", lambda org: []),
    ("org:branch", lambda org: [org.b_mad.pk]),
    ("org:new_branch", lambda org: []),
    ("org:preview_branch_manager", lambda org: [org.b_mad.pk]),
    ("org:confirm_deactivate_branch", lambda org: [org.b_mad.pk]),
    ("org:confirm_reactivate_branch", lambda org: [org.b_mad.pk]),
]
POSTS: list[tuple[str, Callable[[Org], list[object]]]] = [
    ("org:create_company", lambda org: []),
    ("org:update_company", lambda org: [org.b.pk]),
    ("org:deactivate_company", lambda org: [org.b.pk]),
    ("org:reactivate_company", lambda org: [org.b.pk]),
    ("org:add_venture_member", lambda org: [org.b.pk]),
    ("org:create_branch", lambda org: []),
    ("org:update_branch", lambda org: [org.b_mad.pk]),
    ("org:set_branch_manager", lambda org: [org.b_mad.pk]),
    ("org:set_self_approval", lambda org: [org.b_mad.pk]),
    ("org:deactivate_branch", lambda org: [org.b_mad.pk]),
    ("org:reactivate_branch", lambda org: [org.b_mad.pk]),
]


class TestWhoMay:
    @pytest.mark.parametrize("who", ["mm_mad", "engineer", "operator", "tech_mad", "partner"])
    def test_everyone_else_is_refused(self, org: Org, people: People, who: str) -> None:
        client = client_for(getattr(people, who))
        for name, args in GETS:
            assert client.get(reverse(name, args=args(org))).status_code == 403, name
        for name, args in POSTS:
            assert client.post(reverse(name, args=args(org))).status_code == 403, name

    def test_actions_answer_405_to_a_get_and_pages_to_a_post(self, org: Org, admin: Client) -> None:
        for name, args in POSTS:
            assert admin.get(reverse(name, args=args(org))).status_code == 405, name
        for name, args in GETS:
            assert admin.post(reverse(name, args=args(org))).status_code == 405, name

    @pytest.mark.parametrize(
        "name",
        ["org:company", "org:branch", "org:confirm_deactivate_branch", "org:update_branch"],
    )
    def test_an_unknown_object_is_404(self, admin: Client, name: str) -> None:
        method = admin.post if name == "org:update_branch" else admin.get
        assert method(reverse(name, args=[UNKNOWN])).status_code == 404

    def test_the_venture_member_urls(self, org: Org, people: People, admin: Client) -> None:
        member_row = JointVentureMember.objects.create(venture=org.b, member=org.a)
        edit = reverse("org:edit_venture_member", args=[member_row.pk])
        update = reverse("org:update_venture_member", args=[member_row.pk])
        assert client_for(people.mm_mad).get(edit).status_code == 403
        assert client_for(people.mm_mad).post(update).status_code == 403
        assert admin.get(reverse("org:edit_venture_member", args=[UNKNOWN])).status_code == 404

    def test_without_session_mfa_the_administrator_steps_up(self, people: People) -> None:
        client = client_for(people.group_admin, mfa=False)
        assert client.get(reverse("org:companies")).status_code == 302

    def test_a_revoked_administrator_is_refused(self, org: Org, people: People) -> None:
        other = member(Role.GROUP_ADMIN, email="admin-2@example.org")
        enroll_totp(other)
        client = client_for(other)
        page = client.get(reverse("org:confirm_deactivate_branch", args=[org.b_mad.pk]))
        assert page.status_code == 200
        Membership.objects.filter(user=other).update(revoked_at=timezone.now())
        response = client.post(
            reverse("org:deactivate_branch", args=[org.b_mad.pk]),
            {"seen_generation": generation_in(page.content)},
        )
        assert response.status_code == 403
        assert Branch.objects.get(pk=org.b_mad.pk).is_active


# --- companies -------------------------------------------------------------------------------


class TestCompanyList:
    def test_filters_and_partial(self, org: Org, admin: Client) -> None:
        url = reverse("org:companies")
        page = admin.get(url).content.decode()
        for code in ("A", "B", "C", "D"):
            assert f">{code}</a>" in page
        inactive = admin.get(url, {"state": "inactive"}).content.decode()
        assert ">D</a>" in inactive and ">A</a>" not in inactive
        ventures = admin.get(url, {"kind": "JOINT_VENTURE"}).content.decode()
        assert ">B</a>" in ventures and ">C</a>" not in ventures
        assert ">C</a>" in admin.get(url, {"q": "company c"}).content.decode()
        partial = admin.get(url, {"q": "UTE"}, headers=HTMX)
        assert partial.content.lstrip().startswith(b'<div id="company-results">')

    def test_counts_the_branches(self, org: Org, admin: Client) -> None:
        row = admin.get(reverse("org:companies"), {"q": "Company A"}).context["listing"].rows[0]
        assert row.code == "A" and row.branch_count == 4  # HQ, MAD, SEV and OLD


class TestCompanyPage:
    def test_a_joint_venture_shows_its_members_and_branches(self, org: Org, admin: Client) -> None:
        JointVentureMember.objects.create(venture=org.b, member=org.a, share_pct=Decimal("60"))
        page = admin.get(reverse("org:company", args=[org.b.pk])).content.decode()
        assert 'data-testid="venture-member"' in page and "60,00" in page  # Spanish, the default
        assert "B Madrid" in page
        assert reverse("org:confirm_deactivate_company", args=[org.b.pk]) in page
        assert f"{reverse('org:new_branch')}?company={org.b.pk}" in page

    def test_a_standard_company_has_no_members_section(self, org: Org, admin: Client) -> None:
        page = admin.get(reverse("org:company", args=[org.c.pk])).content.decode()
        assert 'id="members"' not in page
        assert reverse("org:add_venture_member", args=[org.c.pk]) not in page

    def test_an_inactive_company_offers_its_reactivation(self, org: Org, admin: Client) -> None:
        d = Company.objects.get(code="D")
        page = admin.get(reverse("org:company", args=[d.pk])).content.decode()
        assert reverse("org:confirm_reactivate_company", args=[d.pk]) in page
        assert reverse("org:new_branch") not in page

    def test_its_branches_page_and_filter(self, org: Org, admin: Client) -> None:
        url = reverse("org:company", args=[org.a.pk])
        inactive = admin.get(url, {"branches-state": "inactive"}).content.decode()
        assert "A closed branch" in inactive and "A Madrid" not in inactive
        response = admin.get(url, {"branches_page": "2"})
        assert response.status_code == 200

    def test_counts_people_with_a_valid_role(self, org: Org, people: People, admin: Client) -> None:
        response = admin.get(reverse("org:company", args=[org.c.pk]))
        assert response.context["people_count"] == 1  # the partner


class TestCreateCompany:
    def test_the_lazy_dialog(self, admin: Client) -> None:
        url = reverse("org:new_company")
        fragment = admin.get(url, headers=in_dialog("new-dialog"))
        assert fragment.status_code == 200 and b"<html" not in fragment.content
        assert b'data-testid="company-new-form"' in fragment.content
        whole = admin.get(url)
        assert b"<html" in whole.content and b'data-testid="company-new-form"' in whole.content

    def test_creates_it_and_its_head_office(self, people: People, admin: Client) -> None:
        response = admin.post(
            reverse("org:create_company"),
            {
                "code": " e ",
                "legal_name": "Company E",
                "tax_id": " ",
                "kind": "STANDARD",
                "idempotency_key": key(people, "company.create"),
            },
            headers=HTMX,
        )
        made = Company.objects.get(code="E")
        assert response.status_code == 204
        assert response["HX-Redirect"] == reverse("org:company", args=[made.pk])
        assert made.tax_id is None and made.branches.get().code == "HQ"
        assert last_event("company.created").actor_id == people.group_admin.pk

    def test_a_second_tap_finds_the_same_company(self, people: People, admin: Client) -> None:
        data = {
            "code": "E",
            "legal_name": "Company E",
            "kind": "STANDARD",
            "idempotency_key": key(people, "company.create"),
        }
        first = admin.post(reverse("org:create_company"), data)
        second = admin.post(reverse("org:create_company"), data)
        assert first.status_code == second.status_code == 302
        assert first["Location"] == second["Location"]
        assert Company.objects.filter(code="E").count() == 1

    def test_a_used_code_comes_back_on_the_form(self, people: People, admin: Client) -> None:
        response = admin.post(
            reverse("org:create_company"),
            {
                "code": "A",
                "legal_name": "Again",
                "kind": "STANDARD",
                "idempotency_key": key(people, "company.create"),
            },
            headers=HTMX,
        )
        assert response.status_code == 422
        assert response.context["d"].form.has_error("code")

    def test_an_empty_form_is_shown_again(self, admin: Client) -> None:
        assert admin.post(reverse("org:create_company"), headers=HTMX).status_code == 422
        plain = admin.post(reverse("org:create_company"))
        assert plain.status_code == 302 and plain["Location"] == reverse("org:companies")


class TestUpdateCompany:
    def test_saves_the_data(self, org: Org, people: People, admin: Client) -> None:
        response = admin.post(
            reverse("org:update_company", args=[org.c.pk]),
            {"legal_name": "Company C S.L.", "tax_id": "B12345678", "kind": "STANDARD"},
            headers=HTMX,
        )
        assert response.status_code == 204
        org.c.refresh_from_db()
        assert (org.c.legal_name, org.c.tax_id, org.c.code) == ("Company C S.L.", "B12345678", "C")
        assert last_event("company.updated").actor_id == people.group_admin.pk

    def test_a_venture_with_members_stays_one(self, org: Org, admin: Client) -> None:
        JointVentureMember.objects.create(venture=org.b, member=org.a)
        response = admin.post(
            reverse("org:update_company", args=[org.b.pk]),
            {"legal_name": "UTE B", "kind": "STANDARD"},
            headers=HTMX,
        )
        assert response.status_code == 422 and codes(response) == {"venture_has_members"}
        assert Company.objects.get(pk=org.b.pk).kind == Company.Kind.JOINT_VENTURE


class TestCompanyActivity:
    def test_the_dialog_previews_whose_access_changes(
        self, org: Org, people: People, admin: Client
    ) -> None:
        response = admin.get(
            reverse("org:confirm_deactivate_company", args=[org.b.pk]),
            headers=in_dialog("activity-dialog"),
        )
        page = response.content.decode()
        assert response.status_code == 200 and 'data-testid="preview"' in page
        assert reverse("org:person", args=[people.site_manager.pk]) in page
        assert reverse("org:person", args=[people.partner.pk]) in page
        assert generation_in(response.content) == org_generation()

    def test_deactivates_and_reactivates(self, org: Org, people: People, admin: Client) -> None:
        before = org_generation()
        response = admin.post(
            reverse("org:deactivate_company", args=[org.b.pk]),
            {"seen_generation": before},
            headers=HTMX,
        )
        assert response.status_code == 204
        assert not Company.objects.get(pk=org.b.pk).is_active
        assert org_generation() == before + 1
        event = last_event("company.updated")
        assert event.actor_id == people.group_admin.pk
        assert event.data["is_active"] == {"from": True, "to": False}
        again = admin.post(
            reverse("org:reactivate_company", args=[org.b.pk]),
            {"seen_generation": org_generation()},
        )
        assert again.status_code == 302 and Company.objects.get(pk=org.b.pk).is_active

    def test_open_documents_refuse_it_in_the_dialog(
        self, org: Org, people: People, admin: Client
    ) -> None:
        issued = order(org, status="ISSUED", issued_by=people.mm_mad)
        dialog = admin.get(reverse("org:confirm_deactivate_company", args=[org.a.pk]))
        assert str(issued.number) in dialog.content.decode()  # said before the attempt
        response = admin.post(
            reverse("org:deactivate_company", args=[org.a.pk]),
            {"seen_generation": generation_in(dialog.content)},
            headers=HTMX,
        )
        assert response.status_code == 422 and str(issued.number) in response.content.decode()
        assert codes(response) == {"open_documents"}
        assert Company.objects.get(pk=org.a.pk).is_active

    def test_a_change_since_the_dialog_answers_409(
        self, org: Org, people: People, admin: Client
    ) -> None:
        dialog = admin.get(reverse("org:confirm_deactivate_company", args=[org.b.pk]))
        Branch.objects.filter(pk=org.a_sev.pk).update(is_active=False)  # someone else, meanwhile
        url = reverse("org:deactivate_company", args=[org.b.pk])
        stale = {"seen_generation": generation_in(dialog.content)}
        response = admin.post(url, stale, headers=HTMX)
        assert response.status_code == 409
        assert b'data-testid="dialog-problem"' in response.content
        plain = admin.post(url, stale)
        assert plain.status_code == 302
        assert Company.objects.get(pk=org.b.pk).is_active
        with translation.override("es"):
            stale_text = gettext(
                "The organization changed after this page was shown. Reload it to see what"
                " this change does now."
            )
        assert stale_text in flashes(admin)

    def test_a_second_submission_changes_nothing_more(self, org: Org, admin: Client) -> None:
        url = reverse("org:deactivate_company", args=[org.b.pk])
        seen = {"seen_generation": org_generation()}
        assert admin.post(url, seen).status_code == 302
        after = org_generation()
        assert admin.post(url, seen).status_code == 302  # already inactive: a no-op
        assert org_generation() == after

    def test_an_invalid_edit_is_shown_again(self, org: Org, admin: Client) -> None:
        url = reverse("org:update_company", args=[org.c.pk])
        response = admin.post(url, {"legal_name": "", "kind": "STANDARD"}, headers=HTMX)
        assert response.status_code == 422 and codes(response) == {"required"}

    def test_a_missing_generation_is_shown_again(self, org: Org, admin: Client) -> None:
        url = reverse("org:deactivate_company", args=[org.b.pk])
        assert admin.post(url, headers=HTMX).status_code == 422
        assert Company.objects.get(pk=org.b.pk).is_active


class TestVentureMembers:
    def test_adds_a_member(self, org: Org, people: People, admin: Client) -> None:
        response = admin.post(
            reverse("org:add_venture_member", args=[org.b.pk]),
            {
                "member": org.c.pk,
                "share_pct": "40",
                "idempotency_key": key(people, "joint_venture_member.add"),
            },
            headers=HTMX,
        )
        assert response.status_code == 204
        added = JointVentureMember.objects.get(venture=org.b)
        assert (added.member_id, added.share_pct) == (org.c.pk, Decimal("40.00"))
        assert last_event("joint_venture_member.added").actor_id == people.group_admin.pk

    def test_a_second_tap_lands_where_the_first_did(
        self, org: Org, people: People, admin: Client
    ) -> None:
        data = {"member": org.c.pk, "idempotency_key": key(people, "joint_venture_member.add")}
        url = reverse("org:add_venture_member", args=[org.b.pk])
        assert admin.post(url, data).status_code == 302
        second = admin.post(url, data, headers=HTMX)  # C is no longer offered
        assert second.status_code == 204
        assert JointVentureMember.objects.filter(venture=org.b).count() == 1

    def test_an_invalid_or_tampered_form_is_shown_again(
        self, org: Org, people: People, admin: Client
    ) -> None:
        url = reverse("org:add_venture_member", args=[org.b.pk])
        assert admin.post(url, {"member": org.c.pk}, headers=HTMX).status_code == 422
        tampered = {"member": "nonsense", "idempotency_key": "not-a-key"}
        assert admin.post(url, tampered, headers=HTMX).status_code == 422
        assert not JointVentureMember.objects.filter(venture=org.b).exists()

    def test_offers_only_other_active_companies(self, org: Org, people: People) -> None:
        from apps.access.context import AccessContext
        from apps.organizations.forms import VentureMemberForm

        JointVentureMember.objects.create(venture=org.b, member=org.a)
        form = VentureMemberForm(ctx=AccessContext.for_user(people.group_admin), venture=org.b)
        offered = {company.code for company in form.fields["member"].queryset}  # type: ignore[attr-defined]
        assert offered == {"C"}  # not B itself, not A (a member), not D (inactive)

    def test_a_standard_company_has_no_members(
        self, org: Org, people: People, admin: Client
    ) -> None:
        response = admin.post(
            reverse("org:add_venture_member", args=[org.c.pk]),
            {"member": org.a.pk, "idempotency_key": key(people, "joint_venture_member.add")},
            headers=HTMX,
        )
        assert response.status_code == 422 and codes(response) == {"not_a_venture"}

    def test_changes_a_share(self, org: Org, people: People, admin: Client) -> None:
        row = JointVentureMember.objects.create(venture=org.b, member=org.a)
        edit = admin.get(
            reverse("org:edit_venture_member", args=[row.pk]), headers=in_dialog("share-dialog")
        )
        assert edit.status_code == 200 and b'data-testid="share-form"' in edit.content
        url = reverse("org:update_venture_member", args=[row.pk])
        assert admin.post(url, {"share_pct": "0"}, headers=HTMX).status_code == 422
        assert admin.post(url, {"share_pct": "55.5"}, headers=HTMX).status_code == 204
        row.refresh_from_db()
        assert row.share_pct == Decimal("55.50")
        assert last_event("joint_venture_member.updated").actor_id == people.group_admin.pk


# --- branches --------------------------------------------------------------------------------


class TestBranchList:
    def test_filters_and_partial(self, org: Org, admin: Client) -> None:
        url = reverse("org:branches")
        assert "B Madrid" in admin.get(url).content.decode()
        of_a = admin.get(url, {"company": str(org.a.pk)}).content.decode()
        assert "A Sevilla" in of_a and "B Madrid" not in of_a
        inactive = admin.get(url, {"state": "inactive"}).content.decode()
        assert "A closed branch" in inactive and "A Sevilla" not in inactive
        assert "A Sevilla" in admin.get(url, {"q": "sevilla"}).content.decode()
        partial = admin.get(url, {"q": "x"}, headers=HTMX)
        assert partial.content.lstrip().startswith(b'<div id="branch-results">')


class TestBranchPage:
    def test_a_managing_branch(self, org: Org, admin: Client) -> None:
        response = admin.get(reverse("org:branch", args=[org.a_mad.pk]))
        page = response.content.decode()
        assert 'data-testid="managed-branch"' in page and "B Madrid" in page
        assert response.context["manager_dialog"] is None  # it manages others: one level
        assert "wh A Madrid" in page
        assert response.context["projects"] == {"executes": 1, "buys": 3}

    def test_a_managed_branch(self, org: Org, admin: Client) -> None:
        response = admin.get(reverse("org:branch", args=[org.b_mad.pk]))
        assert response.context["manager_dialog"] is not None
        offered = response.context["manager_dialog"].form.fields["managed_by"].queryset
        assert org.b_mad not in offered and org.a_sev in offered
        assert (
            reverse("org:preview_branch_manager", args=[org.b_mad.pk]) in response.content.decode()
        )


class TestCreateBranch:
    def test_opened_from_a_company(self, org: Org, admin: Client) -> None:
        response = admin.get(reverse("org:new_branch"), {"company": str(org.c.pk)})
        assert response.context["d"].form.initial["company"] == org.c.pk
        assert response.context["d"].back == reverse("org:company", args=[org.c.pk])
        ignored = admin.get(reverse("org:new_branch"), {"company": "nonsense"})
        assert "company" not in ignored.context["d"].form.initial

    def test_creates_a_branch(self, org: Org, people: People, admin: Client) -> None:
        response = admin.post(
            reverse("org:create_branch"),
            {
                "company": org.a.pk,
                "code": "bcn",
                "name": "A Barcelona",
                "city": "Barcelona",
                "managed_by": org.a_mad.pk,
                "allow_self_approval": "on",
                "idempotency_key": key(people, "branch.create"),
            },
            headers=HTMX,
        )
        made = Branch.objects.get(company=org.a, code="BCN")
        assert response.status_code == 204
        assert response["HX-Redirect"] == reverse("org:branch", args=[made.pk])
        assert made.managed_by_id == org.a_mad.pk and made.allow_self_approval
        assert last_event("branch.created").actor_id == people.group_admin.pk

    def test_an_inactive_company_is_refused(self, org: Org, people: People, admin: Client) -> None:
        from apps.organizations.forms import BranchCreateForm

        d = Company.objects.get(code="D")
        data = {
            "company": d.pk,
            "code": "X",
            "name": "X",
            "idempotency_key": key(people, BranchCreateForm.operation),
        }
        response = admin.post(reverse("org:create_branch"), data, headers=HTMX)
        # Only active companies are offered; the service refuses one again (``inactive_company``).
        assert response.status_code == 422 and codes(response) == {"invalid_choice"}
        assert not Branch.objects.filter(company=d, code="X").exists()

    def test_a_managed_branch_cannot_manage(self, org: Org, people: People, admin: Client) -> None:
        response = admin.post(
            reverse("org:create_branch"),
            {
                "company": org.a.pk,
                "code": "X",
                "name": "X",
                "managed_by": org.b_mad.pk,
                "idempotency_key": key(people, "branch.create"),
            },
            headers=HTMX,
        )
        assert response.status_code == 422  # B-MAD is not offered: a managed branch
        assert response.context["d"].form.has_error("managed_by")


class TestUpdateBranch:
    def test_saves_the_data_and_the_default_warehouse(
        self, org: Org, people: People, admin: Client
    ) -> None:
        warehouse = org.warehouses[org.a_sev.name]
        response = admin.post(
            reverse("org:update_branch", args=[org.a_sev.pk]),
            {"name": "A Sevilla Sur", "city": "Sevilla", "default_warehouse": warehouse.pk},
            headers=HTMX,
        )
        assert response.status_code == 204
        org.a_sev.refresh_from_db()
        assert (org.a_sev.name, org.a_sev.default_warehouse_id) == ("A Sevilla Sur", warehouse.pk)
        assert last_event("branch.updated").actor_id == people.group_admin.pk

    def test_another_branchs_warehouse_is_not_offered(self, org: Org, admin: Client) -> None:
        other = org.warehouses[org.a_mad.name]
        response = admin.post(
            reverse("org:update_branch", args=[org.a_sev.pk]),
            {"name": "A Sevilla", "default_warehouse": other.pk},
            headers=HTMX,
        )
        assert response.status_code == 422
        assert Branch.objects.get(pk=org.a_sev.pk).default_warehouse_id is None

    def test_an_inactive_warehouse_is_not_offered(self, org: Org, admin: Client) -> None:
        closed = Location.objects.create(
            kind=Location.Kind.WAREHOUSE, branch=org.a_sev, name="closed", is_active=False
        )
        response = admin.post(
            reverse("org:update_branch", args=[org.a_sev.pk]),
            {"name": "A Sevilla", "default_warehouse": closed.pk},
            headers=HTMX,
        )
        assert response.status_code == 422


class TestManagingBranch:
    def test_the_preview_follows_the_choice(self, org: Org, people: People, admin: Client) -> None:
        url = reverse("org:preview_branch_manager", args=[org.b_mad.pk])
        response = admin.get(url, {"managed_by": str(org.a_sev.pk)}, headers=HTMX)
        page = response.content.decode()
        assert 'data-testid="preview-joining"' in page and "A-SEV" in page
        assert 'hx-swap-oob="true"' in page and 'id="id_manager_seen_generation"' in page
        assert f'value="{org_generation()}"' in page
        none = admin.get(url, {"managed_by": ""}, headers=HTMX).content.decode()
        assert 'data-testid="preview-leaving"' in none
        for value in (str(org.b_mad.pk), "nonsense", UNKNOWN):  # never itself, never unknown
            refused = admin.get(url, {"managed_by": value}, headers=HTMX).content.decode()
            assert 'data-testid="preview"' not in refused

    def test_changes_it(self, org: Org, people: People, admin: Client) -> None:
        before = org_generation()
        response = admin.post(
            reverse("org:set_branch_manager", args=[org.b_mad.pk]),
            {"managed_by": org.a_sev.pk, "seen_generation": before},
            headers=HTMX,
        )
        assert response.status_code == 204
        assert Branch.objects.get(pk=org.b_mad.pk).managed_by_id == org.a_sev.pk
        assert org_generation() == before + 1
        event = last_event("branch.updated")
        assert event.actor_id == people.group_admin.pk and "managed_by_id" in event.data

    def test_a_change_since_the_preview_answers_409(self, org: Org, admin: Client) -> None:
        seen = org_generation()
        Company.objects.filter(code="D").update(is_active=True)
        response = admin.post(
            reverse("org:set_branch_manager", args=[org.b_mad.pk]),
            {"managed_by": org.a_sev.pk, "seen_generation": seen},
            headers=HTMX,
        )
        assert response.status_code == 409
        assert Branch.objects.get(pk=org.b_mad.pk).managed_by_id == org.a_mad.pk

    def test_a_branch_that_manages_others_is_offered_nothing(self, org: Org, admin: Client) -> None:
        response = admin.post(
            reverse("org:set_branch_manager", args=[org.a_mad.pk]),
            {"managed_by": org.a_sev.pk, "seen_generation": org_generation()},
            headers=HTMX,
        )
        assert response.status_code == 422
        assert Branch.objects.get(pk=org.a_mad.pk).managed_by_id is None


class TestSelfApproval:
    def test_turns_it_on_and_off(self, org: Org, people: People, admin: Client) -> None:
        page = admin.get(reverse("org:branch", args=[org.a_sev.pk]))
        dialog = page.context["approval_dialog"]
        assert dialog.form.initial == {"allow": "on"} and len(dialog.consequences) == 2
        url = reverse("org:set_self_approval", args=[org.a_sev.pk])
        assert admin.post(url, {"allow": "on"}, headers=HTMX).status_code == 204
        assert Branch.objects.get(pk=org.a_sev.pk).allow_self_approval
        assert last_event("branch.updated").data["allow_self_approval"]["to"] is True
        assert admin.post(url, {"allow": "on"}).status_code == 302  # the same value again
        assert admin.post(url, {"allow": "off"}).status_code == 302
        assert not Branch.objects.get(pk=org.a_sev.pk).allow_self_approval

    def test_an_unknown_value_is_refused(self, org: Org, admin: Client) -> None:
        url = reverse("org:set_self_approval", args=[org.a_sev.pk])
        assert admin.post(url, {"allow": "maybe"}, headers=HTMX).status_code == 422


class TestBranchActivity:
    def test_deactivates_with_its_preview(self, org: Org, people: People, admin: Client) -> None:
        dialog = admin.get(
            reverse("org:confirm_deactivate_branch", args=[org.b_mad.pk]),
            headers=in_dialog("activity-dialog"),
        )
        assert b'data-testid="preview-losing"' in dialog.content
        response = admin.post(
            reverse("org:deactivate_branch", args=[org.b_mad.pk]),
            {"seen_generation": generation_in(dialog.content)},
            headers=HTMX,
        )
        assert response.status_code == 204
        assert not Branch.objects.get(pk=org.b_mad.pk).is_active
        assert last_event("branch.updated").actor_id == people.group_admin.pk

    def test_a_missing_generation_is_shown_again(self, org: Org, admin: Client) -> None:
        url = reverse("org:deactivate_branch", args=[org.b_mad.pk])
        response = admin.post(url, {"seen_generation": "-1"}, headers=HTMX)
        assert response.status_code == 422
        assert Branch.objects.get(pk=org.b_mad.pk).is_active

    def test_reactivating_under_an_inactive_company_warns(self, org: Org, admin: Client) -> None:
        d_hq = Branch.objects.get(company__code="D")
        page = admin.get(reverse("org:confirm_reactivate_branch", args=[d_hq.pk])).content.decode()
        assert 'data-testid="activity-warning"' in page
        response = admin.post(
            reverse("org:reactivate_branch", args=[d_hq.pk]),
            {"seen_generation": org_generation()},
        )
        assert response.status_code == 302 and Branch.objects.get(pk=d_hq.pk).is_active

    def test_open_documents_refuse_it(self, org: Org, people: People, admin: Client) -> None:
        issued = order(org, status="ISSUED", issued_by=people.mm_mad)
        response = admin.post(
            reverse("org:deactivate_branch", args=[org.a_mad.pk]),
            {"seen_generation": org_generation()},
            headers=HTMX,
        )
        assert response.status_code == 422 and str(issued.number) in response.content.decode()
        assert response.context["d"].data().blocking  # the preview is read again, for the dialog
        assert Branch.objects.get(pk=org.a_mad.pk).is_active

    def test_the_default_warehouse_must_be_its_own(self, org: Org, admin: Client) -> None:
        """The service's check, reached through the edit form by a forged choice."""
        Branch.objects.filter(pk=org.a_sev.pk).update(
            default_warehouse=org.warehouses[org.a_sev.name]
        )
        response = admin.post(
            reverse("org:update_branch", args=[org.a_sev.pk]),
            {"name": "A Sevilla", "default_warehouse": ""},
        )
        assert response.status_code == 302
        assert Branch.objects.get(pk=org.a_sev.pk).default_warehouse_id is None


def test_every_page_renders_for_the_administrator(org: Org, admin: Client) -> None:
    for name, args in GETS:
        response = admin.get(reverse(name, args=args(org)))
        assert response.status_code == 200, name
        assert "no-store" in response["Cache-Control"], name

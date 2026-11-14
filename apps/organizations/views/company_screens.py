"""Companies and joint ventures (M10.2): list, page, create, edit, deactivate, reactivate, and
the members of a joint venture.

Every page and action needs ``org.manage`` (403 otherwise; an unknown company is 404). Reads
go through the selectors; writes through ``apps.organizations.services`` from POST-only views,
each on its own URL, answering by the dialog contract of ``apps.core.responses``. Deactivating
or reactivating opens a lazy danger dialog that previews whose access changes; the service
decides (open documents, an organization changed since the preview).
"""

from urllib.parse import urlencode
from uuid import UUID

from django.contrib import messages
from django.http import HttpRequest, HttpResponse
from django.shortcuts import render
from django.urls import reverse
from django.utils.translation import gettext as _
from django.views.decorators.http import require_safe

from apps.access.middleware import access_for
from apps.access.mixins import mutating, require_capability
from apps.core.dialogs import lazy
from apps.core.listing import ListScope, paginate, render_list
from apps.core.responses import go, invalid, perform
from apps.organizations import selectors, services
from apps.organizations.forms import (
    CompanyCreateForm,
    CompanyFilterForm,
    CompanyForm,
    ScopeChangeForm,
    StateFilterForm,
    VentureMemberForm,
    VentureShareForm,
)
from apps.organizations.models import Company
from apps.organizations.structure_dialogs import (
    company_activity_dialog,
    company_edit_dialog,
    company_url,
    new_company_dialog,
    venture_member_dialog,
    venture_share_dialog,
)
from apps.organizations.views.structure_common import ORG, already_done, section

COMPANY_BRANCHES = ListScope(prefix="branches_", anchor="branches")
VENTURE_MEMBERS = ListScope(prefix="members_", anchor="members", sized=False)

NEW_IDS = "id_new_%s"
EDIT_IDS = "id_company_%s"
ACTIVITY_IDS = "id_activity_%s"
MEMBER_IDS = "id_member_%s"
SHARE_IDS = "id_share_%s"


@require_safe
@require_capability(ORG)
def companies(request: HttpRequest) -> HttpResponse:
    ctx = access_for(request)
    filters = CompanyFilterForm(request.GET or None)
    data = filters.cleaned_data if filters.is_valid() else {}
    listing = paginate(
        request,
        selectors.company_list(
            ctx, q=data.get("q", ""), state=data.get("state", ""), kind=data.get("kind", "")
        ),
        default_per_page=selectors.DEFAULT_PER_PAGE,
        sorts=selectors.COMPANY_SORTS,
        default_sort="code",
        sort_labels=selectors.COMPANY_SORT_LABELS,
    )
    return render_list(
        request, "organizations/companies.html", {"filters": filters, "listing": listing}
    )


@require_safe
@require_capability(ORG)
def company(request: HttpRequest, company_id: UUID) -> HttpResponse:
    ctx = access_for(request)
    found = selectors.company(ctx, company_id)
    branch_filters, branches = section(
        request,
        lambda **narrow: selectors.company_branches(ctx, found, **narrow),
        COMPANY_BRANCHES,
        StateFilterForm,
    )
    venture = found.kind == Company.Kind.JOINT_VENTURE
    context = {
        "company": found,
        "people_count": selectors.people_count(ctx, found),
        "new_branch_url": f"{reverse('org:new_branch')}?{urlencode({'company': found.pk})}",
        "branch_filters": branch_filters,
        "branches": branches,
        "members": paginate(
            request,
            selectors.venture_members(ctx, found),
            default_per_page=selectors.SECTION_PER_PAGE,
            scope=VENTURE_MEMBERS,
        )
        if venture
        else None,
        "edit_dialog": company_edit_dialog(
            found, CompanyForm(initial=CompanyForm.initial_for(found), auto_id=EDIT_IDS)
        ),
        "member_dialog": venture_member_dialog(
            found,
            VentureMemberForm(ctx=ctx, venture=found, actor_id=ctx.user_id, auto_id=MEMBER_IDS),
        )
        if venture
        else None,
    }
    return render(request, "organizations/company.html", context)


@require_safe
@require_capability(ORG)
def new_company(request: HttpRequest) -> HttpResponse:
    """The dialog that creates a company (its POST is ``create_company``)."""
    form = CompanyCreateForm(actor_id=access_for(request).user_id, auto_id=NEW_IDS)
    return lazy(request, new_company_dialog(form))


@mutating(ORG)
def create_company(request: HttpRequest) -> HttpResponse:
    ctx = access_for(request)
    form = CompanyCreateForm(request.POST, auto_id=NEW_IDS)
    dialog = new_company_dialog(form).dialog()
    to = reverse("org:companies")
    if not form.is_valid():
        return invalid(request, form, to, dialog=dialog)
    return perform(
        request,
        lambda: services.create_company(ctx.principal, **form.create_kwargs()),
        lambda made: _("Company %(code)s created.") % {"code": made.code},
        lambda made: company_url(made.pk),
        failed_to=to,
        dialog=dialog,
    )


@mutating(ORG)
def update_company(request: HttpRequest, company_id: UUID) -> HttpResponse:
    ctx = access_for(request)
    found = selectors.company(ctx, company_id)
    form = CompanyForm(request.POST, auto_id=EDIT_IDS)
    dialog = company_edit_dialog(found, form).dialog()
    to = company_url(found.pk)
    if not form.is_valid():
        return invalid(request, form, to, dialog=dialog)
    return perform(
        request,
        lambda: services.update_company(ctx.principal, found.pk, **form.values()),
        _("Company saved."),
        to,
        dialog=dialog,
    )


# --- activity --------------------------------------------------------------------------------


def _confirm(request: HttpRequest, company_id: UUID, *, active: bool) -> HttpResponse:
    """The lazy dialog of an activity change, with its preview read now."""
    ctx = access_for(request)
    found = selectors.company(ctx, company_id)
    preview = selectors.company_activity(ctx, found, active=active)
    form = ScopeChangeForm(initial={"seen_generation": preview.generation}, auto_id=ACTIVITY_IDS)
    return lazy(request, company_activity_dialog(found, form, active=active, preview=preview))


@require_safe
@require_capability(ORG)
def confirm_deactivate_company(request: HttpRequest, company_id: UUID) -> HttpResponse:
    return _confirm(request, company_id, active=False)


@require_safe
@require_capability(ORG)
def confirm_reactivate_company(request: HttpRequest, company_id: UUID) -> HttpResponse:
    return _confirm(request, company_id, active=True)


def _set_active(request: HttpRequest, company_id: UUID, *, active: bool) -> HttpResponse:
    ctx = access_for(request)
    found = selectors.company(ctx, company_id)
    form = ScopeChangeForm(request.POST, auto_id=ACTIVITY_IDS)
    dialog = company_activity_dialog(
        found,
        form,
        active=active,
        preview=lambda: selectors.company_activity(ctx, found, active=active),
    ).dialog()
    to = company_url(found.pk)
    if not form.is_valid():
        return invalid(request, form, to, dialog=dialog)
    done = _("Company reactivated.") if active else _("Company deactivated.")
    return perform(
        request,
        lambda: services.update_company(
            ctx.principal,
            found.pk,
            is_active=active,
            seen_generation=form.cleaned_data["seen_generation"],
        ),
        done,
        to,
        dialog=dialog,
    )


@mutating(ORG)
def deactivate_company(request: HttpRequest, company_id: UUID) -> HttpResponse:
    return _set_active(request, company_id, active=False)


@mutating(ORG)
def reactivate_company(request: HttpRequest, company_id: UUID) -> HttpResponse:
    return _set_active(request, company_id, active=True)


# --- joint-venture members -----------------------------------------------------------------


@mutating(ORG)
def add_venture_member(request: HttpRequest, company_id: UUID) -> HttpResponse:
    ctx = access_for(request)
    venture = selectors.company(ctx, company_id)
    form = VentureMemberForm(request.POST, ctx=ctx, venture=venture, auto_id=MEMBER_IDS)
    dialog = venture_member_dialog(venture, form).dialog()
    to = company_url(venture.pk)
    if not form.is_valid():
        if already_done(request, form):
            messages.success(request, _("Member added."))
            return go(request, to)
        return invalid(request, form, to, dialog=dialog)
    data = form.cleaned_data
    return perform(
        request,
        lambda: services.add_joint_venture_member(
            ctx.principal,
            venture_id=venture.pk,
            member_id=data["member"].pk,
            share_pct=data["share_pct"],
            idempotency_key=data["idempotency_key"],
        ),
        _("Member added."),
        to,
        dialog=dialog,
    )


@require_safe
@require_capability(ORG)
def edit_venture_member(request: HttpRequest, member_id: UUID) -> HttpResponse:
    """The dialog that changes a member's share (its POST is ``update_venture_member``)."""
    member = selectors.venture_member(access_for(request), member_id)
    form = VentureShareForm(initial={"share_pct": member.share_pct}, auto_id=SHARE_IDS)
    return lazy(request, venture_share_dialog(member, form))


@mutating(ORG)
def update_venture_member(request: HttpRequest, member_id: UUID) -> HttpResponse:
    ctx = access_for(request)
    member = selectors.venture_member(ctx, member_id)
    form = VentureShareForm(request.POST, auto_id=SHARE_IDS)
    dialog = venture_share_dialog(member, form).dialog()
    to = company_url(member.venture_id)
    if not form.is_valid():
        return invalid(request, form, to, dialog=dialog)
    return perform(
        request,
        lambda: services.update_joint_venture_member(
            ctx.principal, member.pk, share_pct=form.cleaned_data["share_pct"]
        ),
        _("Share saved."),
        to,
        dialog=dialog,
    )

"""Group-administrator screens (plan §3.4, §5, M2.6): people, memberships, offboarding,
email changes and invitations.

Every screen needs ``org.manage`` (403 otherwise). Reads go through the selectors; writes
through the services, from POST-only views on their own URLs. A staff or superuser account
is shown to a group administrator but every action on it is refused by the services
(plan §3.4); the page hides the forms.
"""

from collections.abc import Callable, Iterable
from typing import Any
from uuid import UUID

from django import forms
from django.contrib import messages
from django.core.exceptions import ValidationError
from django.core.paginator import Page
from django.http import HttpRequest, HttpResponse
from django.shortcuts import redirect, render
from django.utils import timezone
from django.utils.translation import gettext as _
from django.views.decorators.http import require_safe

from apps.access.context import AccessContext
from apps.access.middleware import access_for
from apps.access.mixins import mutating, require_capability
from apps.access.policy import may_manage_account
from apps.access.roles import Capability
from apps.accounts import selectors as account_selectors
from apps.accounts import services as account_services
from apps.accounts.models import EmailChangeRequest, Invitation, User
from apps.accounts.services import (
    EMAIL_CHANGE_CONFIRM_MAIL,
    EMAIL_CHANGE_NOTICE_MAIL,
    INVITATION_MAIL,
)
from apps.accounts.sessions import session_passive
from apps.core.dialogs import lazy
from apps.core.forms import ValidityForm, add_service_errors, error_text, form_error_text
from apps.core.idempotency import IdempotencyError
from apps.core.listing import ListPage, ListScope, paginate, render_list
from apps.core.locking import RetryExhausted
from apps.core.responses import conflict, idempotency_failure, invalid, perform
from apps.notifications import selectors as notification_selectors
from apps.organizations import selectors, services
from apps.organizations.forms import (
    ConfirmForm,
    EmailChangeForm,
    EmployerForm,
    InvitationFilterForm,
    InvitationForm,
    MembershipFilterForm,
    MembershipForm,
    PeopleFilterForm,
    PersonInvitationFilterForm,
    ReasonForm,
    SharedProjectFilterForm,
)
from apps.organizations.row_dialogs import (
    grant_dialog,
    membership_revoke_dialog,
    membership_validity_dialog,
    offboard_dialog,
    person_url,
    revoke_email_change_dialog,
)

ORG = Capability.ORG_MANAGE


def _with_delivery(ctx: AccessContext, invitations: Iterable[Invitation]) -> list[Invitation]:
    """The invitations read through the scoped selector, each with its email's status.

    ``delivery`` stays ``None`` for an invitation that no longer waits for an answer
    (accepted, revoked or expired) and for one that sent no email (the bootstrap link).
    """
    rows = list(invitations)
    current = timezone.now()
    pending = [
        row.pk
        for row in rows
        if row.accepted_at is None and row.revoked_at is None and row.expires_at > current
    ]
    found = notification_selectors.delivery_status(ctx, notification_selectors.INVITATION, pending)
    for row in rows:
        row.delivery = found.get((row.pk, INVITATION_MAIL))  # type: ignore[attr-defined]
    return rows


def _with_delivery_page(ctx: AccessContext, page: Page[Invitation]) -> None:
    """Give each invitation of the page its delivery status, read in one query."""
    page.object_list = _with_delivery(ctx, page.object_list)


def _change_delivery(ctx: AccessContext, change: EmailChangeRequest) -> dict[str, object]:
    """An email change with the status of its two emails: to the new address, to the old one."""
    found = notification_selectors.delivery_status(
        ctx, notification_selectors.EMAIL_CHANGE, [change.pk]
    )
    return {
        "change": change,
        "confirm": found.get((change.pk, EMAIL_CHANGE_CONFIRM_MAIL)),
        "notice": found.get((change.pk, EMAIL_CHANGE_NOTICE_MAIL)),
    }


# --- people ----------------------------------------------------------------------------


@require_safe
@require_capability(ORG)
def people(request: HttpRequest) -> HttpResponse:
    ctx = access_for(request)
    filters = PeopleFilterForm(request.GET or None, ctx=ctx)
    data = filters.cleaned_data if filters.is_valid() else {}
    branch = data.get("branch")
    listing = paginate(
        request,
        account_selectors.people(
            ctx,
            q=data.get("q", ""),
            status=data.get("status", ""),
            role=data.get("role", ""),
            branch_id=branch.pk if branch else None,
        ),
        default_per_page=account_selectors.DEFAULT_PER_PAGE,
        sorts=account_selectors.PEOPLE_SORTS,
        default_sort="email",
        sort_labels=account_selectors.PEOPLE_SORT_LABELS,
    )
    return render_list(
        request, "organizations/people.html", {"filters": filters, "listing": listing}
    )


MEMBERSHIPS_SCOPE = ListScope(prefix="memberships_", anchor="roles")
PROJECTS_SCOPE = ListScope(prefix="projects_", anchor="projects")
INVITATIONS_SCOPE = ListScope(prefix="invitations_", anchor="invitations")


def _section(
    request: HttpRequest, rows: Callable[..., Any], form: type[forms.Form], scope: ListScope
) -> tuple[forms.Form, ListPage]:
    """A paged, filtered section of a person's page: ``rows(**cleaned_data)`` paged by
    ``scope``. The filter fields are named after the section (``memberships-state``)."""
    filters = form(request.GET or None, prefix=scope.prefix.removesuffix("_"))
    data = filters.cleaned_data if filters.is_valid() else {}
    listing = paginate(
        request,
        rows(**data),
        default_per_page=account_selectors.SECTION_PER_PAGE,
        scope=scope,
    )
    return filters, listing


def _person_sections(request: HttpRequest, ctx: AccessContext, user: User) -> dict[str, Any]:
    """The roles, shared projects and invitations of a person, each paged on its own."""
    roles, role_page = _section(
        request,
        lambda **narrow: selectors.memberships(ctx, user, **narrow),
        MembershipFilterForm,
        MEMBERSHIPS_SCOPE,
    )
    shared, shared_page = _section(
        request,
        lambda **narrow: account_selectors.assignments_of(ctx, user, **narrow),
        SharedProjectFilterForm,
        PROJECTS_SCOPE,
    )
    invited, invited_page = _section(
        request,
        lambda **narrow: account_selectors.invitations(ctx, of=user, pending_only=False, **narrow),
        PersonInvitationFilterForm,
        INVITATIONS_SCOPE,
    )
    _with_delivery_page(ctx, invited_page.page)
    return {
        "membership_filters": roles,
        "memberships": role_page,
        "assignment_filters": shared,
        "assignments": shared_page,
        "invitation_filters": invited,
        "invitations": invited_page,
    }


def _email_revoke(change: EmailChangeRequest | None) -> Any:
    """The dialog that revokes the open email change, if there is one."""
    if change is None:
        return None
    return revoke_email_change_dialog(change, ReasonForm(auto_id=EMAIL_REVOKE_IDS))


@require_safe
@require_capability(ORG)
def person(request: HttpRequest, user_id: UUID) -> HttpResponse:
    ctx = access_for(request)
    user = account_selectors.person(ctx, user_id)
    open_change = account_selectors.email_changes(ctx, user).filter(
        applied_at__isnull=True, cancelled_at__isnull=True, revoked_at__isnull=True
    )
    change = open_change.first()
    context = {
        "person": user,
        "manageable": may_manage_account(request.user, user) and user.pk != ctx.user_id,
        # One's own employer is the one thing an administrator changes on their own page.
        "employer_editable": may_manage_account(request.user, user),
        "is_self": user.pk == ctx.user_id,
        **_person_sections(request, ctx, user),
        "open_change": change,
        "change_delivery": None if change is None else _change_delivery(ctx, change),
        "grant_dialog": grant_dialog(
            user, MembershipForm(ctx=ctx, actor_id=ctx.user_id, auto_id=GRANT_IDS)
        ),
        "employer_form": EmployerForm(initial={"company": user.employer_company_id}, ctx=ctx),
        "email_form": EmailChangeForm(),
        "offboard_dialog": offboard_dialog(user, ConfirmForm(auto_id=OFFBOARD_IDS)),
        "email_revoke_dialog": _email_revoke(change),
    }
    return render(request, "organizations/person.html", context)


def _back(user_id: UUID) -> HttpResponse:
    return redirect("org:person", user_id=user_id)


GRANT_IDS = "id_grant_%s"
VALIDITY_IDS = "id_validity_%s"
OFFBOARD_IDS = "id_offboard_%s"
EMAIL_REVOKE_IDS = "id_email_revoke_%s"


@mutating(ORG)
def grant_membership(request: HttpRequest, user_id: UUID) -> HttpResponse:
    ctx = access_for(request)
    user = account_selectors.person(ctx, user_id)
    form = MembershipForm(request.POST, ctx=ctx, auto_id=GRANT_IDS)
    dialog = grant_dialog(user, form).dialog()
    to = person_url(user.pk)
    if not form.is_valid():
        return invalid(request, form, to, dialog=dialog)
    return perform(
        request,
        lambda: services.grant_membership(ctx.principal, user_id=user.pk, **form.grant_kwargs()),
        _("Role granted."),
        to,
        dialog=dialog,
    )


@require_safe
@require_capability(ORG)
def membership_validity_form(request: HttpRequest, membership_id: UUID) -> HttpResponse:
    """The dialog that changes a role's validity (its POST is ``membership_validity``)."""
    membership = selectors.membership(access_for(request), membership_id)
    form = ValidityForm(
        initial={"valid_from": membership.valid_from, "valid_until": membership.valid_until},
        auto_id=VALIDITY_IDS,
    )
    return lazy(request, membership_validity_dialog(membership, form))


@require_safe
@require_capability(ORG)
def membership_revoke_form(request: HttpRequest, membership_id: UUID) -> HttpResponse:
    """The dialog that confirms revoking a role (its POST is ``revoke_membership``)."""
    membership = selectors.membership(access_for(request), membership_id)
    return lazy(request, membership_revoke_dialog(membership, forms.Form()))


@mutating(ORG)
def revoke_membership(request: HttpRequest, membership_id: UUID) -> HttpResponse:
    ctx = access_for(request)
    membership = selectors.membership(ctx, membership_id)
    form = forms.Form(request.POST)
    form.is_valid()  # no fields: a service refusal can then be shown on it
    return perform(
        request,
        lambda: services.revoke_membership(ctx.principal, membership.pk),
        _("Role revoked."),
        person_url(membership.user_id),
        dialog=membership_revoke_dialog(membership, form).dialog(),
    )


@mutating(ORG)
def membership_validity(request: HttpRequest, membership_id: UUID) -> HttpResponse:
    ctx = access_for(request)
    membership = selectors.membership(ctx, membership_id)
    form = ValidityForm(request.POST, auto_id=VALIDITY_IDS)
    dialog = membership_validity_dialog(membership, form).dialog()
    to = person_url(membership.user_id)
    if not form.is_valid():
        return invalid(request, form, to, dialog=dialog)
    return perform(
        request,
        lambda: services.change_membership_validity(
            ctx.principal,
            membership.pk,
            valid_from=form.cleaned_data["valid_from"],
            valid_until=form.cleaned_data["valid_until"],
        ),
        _("Validity saved."),
        to,
        dialog=dialog,
    )


@mutating(ORG)
def set_employer(request: HttpRequest, user_id: UUID) -> HttpResponse:
    ctx = access_for(request)
    user = account_selectors.person(ctx, user_id)
    form = EmployerForm(request.POST, ctx=ctx)
    if not form.is_valid():
        messages.error(request, form_error_text(form))
        return _back(user.pk)
    company = form.cleaned_data["company"]
    try:
        services.set_employer_company(ctx.principal, user.pk, company.pk if company else None)
    except ValidationError as exc:
        messages.error(request, error_text(exc))
    except RetryExhausted:
        return conflict(request)
    else:
        messages.success(request, _("Employer saved."))
    return _back(user.pk)


@mutating(ORG)
def offboard(request: HttpRequest, user_id: UUID) -> HttpResponse:
    ctx = access_for(request)
    user = account_selectors.person(ctx, user_id)
    form = ConfirmForm(request.POST, auto_id=OFFBOARD_IDS)
    dialog = offboard_dialog(user, form).dialog()
    to = person_url(user.pk)
    if not form.is_valid():
        return invalid(request, form, to, dialog=dialog)
    return perform(
        request,
        lambda: account_services.offboard_user(ctx.principal, user.pk),
        _("Account offboarded."),
        to,
        dialog=dialog,
    )


@mutating(ORG)
def request_email_change(request: HttpRequest, user_id: UUID) -> HttpResponse:
    ctx = access_for(request)
    user = account_selectors.person(ctx, user_id)
    form = EmailChangeForm(request.POST)
    if not form.is_valid():
        messages.error(request, form_error_text(form))
        return _back(user.pk)
    try:
        account_services.request_email_change(
            ctx.principal, user.pk, new_email=form.cleaned_data["new_email"]
        )
    except ValidationError as exc:
        messages.error(request, error_text(exc))
    except RetryExhausted:
        return conflict(request)
    else:
        messages.success(
            request,
            _("Confirmation queued for sending to the new address; the current one can cancel."),
        )
    return _back(user.pk)


@mutating(ORG)
def revoke_email_change(request: HttpRequest, change_id: UUID) -> HttpResponse:
    ctx = access_for(request)
    change = account_selectors.email_change(ctx, change_id)
    form = ReasonForm(request.POST, auto_id=EMAIL_REVOKE_IDS)
    reason = form.cleaned_data["reason"] if form.is_valid() else ""
    return perform(
        request,
        lambda: account_services.revoke_email_change(ctx.principal, change.pk, reason=reason),
        _("Email change revoked."),
        person_url(change.user_id),
        dialog=revoke_email_change_dialog(change, form).dialog(),
    )


# --- invitations -----------------------------------------------------------------------


@require_safe
@require_capability(ORG)
def invitations(request: HttpRequest) -> HttpResponse:
    ctx = access_for(request)
    filters = InvitationFilterForm(request.GET or None)
    data = filters.cleaned_data if filters.is_valid() else {}
    listing = paginate(
        request,
        account_selectors.invitations(
            ctx,
            q=data.get("q", ""),
            state=data.get("state", ""),
            delivery=data.get("delivery", ""),
        ),
        default_per_page=account_selectors.DEFAULT_PER_PAGE,
        sorts=account_selectors.INVITATION_SORTS,
        default_sort="-created",
        sort_labels=account_selectors.INVITATION_SORT_LABELS,
    )
    _with_delivery_page(ctx, listing.page)
    return render_list(
        request, "organizations/invitations.html", {"filters": filters, "listing": listing}
    )


@require_safe
@require_capability(ORG)
def new_invitation(request: HttpRequest) -> HttpResponse:
    ctx = access_for(request)
    form = InvitationForm(ctx=ctx, actor_id=ctx.user_id)
    return render(request, "organizations/invitation_form.html", {"form": form})


@mutating(ORG)
def create_invitation(request: HttpRequest) -> HttpResponse:
    ctx = access_for(request)
    form = InvitationForm(request.POST, ctx=ctx)
    if form.is_valid():
        try:
            invitation = account_services.create_invitation(
                ctx.principal, **form.invitation_kwargs()
            )
        except ValidationError as exc:
            add_service_errors(form, exc)
        except IdempotencyError as exc:
            return idempotency_failure(request, exc)
        except RetryExhausted:
            return conflict(request)
        else:
            messages.success(
                request,
                _("Invitation queued for sending to %(email)s.") % {"email": invitation.email},
            )
            return redirect("org:invitations")
    return render(request, "organizations/invitation_form.html", {"form": form})


@mutating(ORG)
def resend_invitation(request: HttpRequest, invitation_id: UUID) -> HttpResponse:
    ctx = access_for(request)
    invitation = account_selectors.invitation(ctx, invitation_id)
    try:
        account_services.resend_invitation(ctx.principal, invitation.pk)
    except ValidationError as exc:
        messages.error(request, error_text(exc))
    except RetryExhausted:
        return conflict(request)
    else:
        messages.success(
            request, _("Invitation queued for sending again; the previous link no longer works.")
        )
    return redirect("org:invitations")


@mutating(ORG)
def revoke_invitation(request: HttpRequest, invitation_id: UUID) -> HttpResponse:
    ctx = access_for(request)
    invitation = account_selectors.invitation(ctx, invitation_id)
    form = ReasonForm(request.POST)
    reason = form.cleaned_data["reason"] if form.is_valid() else ""
    try:
        account_services.revoke_invitation(ctx.principal, invitation.pk, reason=reason)
    except ValidationError as exc:
        messages.error(request, error_text(exc))
    except RetryExhausted:
        return conflict(request)
    else:
        messages.success(request, _("Invitation revoked."))
    return redirect("org:invitations")


# --- delivery status fragments (M7c.1) ---------------------------------------------------


@session_passive
@require_safe
@require_capability(ORG)
def invitation_delivery(request: HttpRequest, invitation_id: UUID) -> HttpResponse:
    """The badge of one invitation's email, polled while the message is in flight."""
    ctx = access_for(request)
    invitation = account_selectors.invitation(ctx, invitation_id)
    (row,) = _with_delivery(ctx, [invitation])
    return render(request, "organizations/delivery_invitation.html", {"invitation": row})


@session_passive
@require_safe
@require_capability(ORG)
def email_change_delivery(request: HttpRequest, change_id: UUID) -> HttpResponse:
    """The badges of the two emails of an email change, polled while one is in flight."""
    ctx = access_for(request)
    change = account_selectors.email_change(ctx, change_id)
    return render(
        request, "organizations/delivery_email_change.html", _change_delivery(ctx, change)
    )

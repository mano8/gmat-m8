"""Group-administrator screens (plan §3.4, §5, M2.6): people, memberships, offboarding,
email changes and invitations. Companies and branches stay in the admin (service-backed).

Every screen needs ``org.manage`` (403 otherwise). Reads go through the selectors; writes
through the services, from POST-only views on their own URLs. A staff or superuser account
is shown to a group administrator but every action on it is refused by the services
(plan §3.4); the page hides the forms.
"""

from collections.abc import Iterable
from urllib.parse import urlencode
from uuid import UUID

from django.contrib import messages
from django.core.exceptions import ValidationError
from django.core.paginator import Paginator
from django.http import HttpRequest, HttpResponse
from django.shortcuts import redirect, render
from django.utils.translation import gettext as _
from django.views.decorators.http import require_safe

from apps.access.context import AccessContext
from apps.access.middleware import access_for
from apps.access.mixins import mutating, require_capability
from apps.access.policy import may_manage_account
from apps.access.roles import Capability
from apps.accounts import selectors as account_selectors
from apps.accounts import services as account_services
from apps.accounts.models import EmailChangeRequest, Invitation
from apps.core.forms import ValidityForm, add_service_errors, error_text, form_error_text
from apps.core.http import is_htmx
from apps.core.idempotency import IdempotencyError
from apps.core.locking import RetryExhausted
from apps.core.responses import conflict, idempotency_failure
from apps.notifications import selectors as notification_selectors
from apps.organizations import selectors, services
from apps.organizations.forms import (
    ConfirmForm,
    EmailChangeForm,
    EmployerForm,
    InvitationFilterForm,
    InvitationForm,
    MembershipForm,
    PeopleFilterForm,
    ReasonForm,
)

ORG = Capability.ORG_MANAGE


def _extra_query(**params: str) -> str:
    kept = {key: value for key, value in params.items() if value}
    return f"&{urlencode(kept)}" if kept else ""


def _with_delivery(ctx: AccessContext, invitations: Iterable[Invitation]) -> list[Invitation]:
    """The invitations read through the scoped selector, each with its email's status.

    ``delivery`` stays ``None`` for an invitation that no longer waits for an answer and for
    one that sent no email (the bootstrap link).
    """
    rows = list(invitations)
    pending = [row.pk for row in rows if row.accepted_at is None and row.revoked_at is None]
    found = notification_selectors.delivery_status(ctx, notification_selectors.INVITATION, pending)
    for row in rows:
        row.delivery = found.get(row.pk)  # type: ignore[attr-defined]
    return rows


def _change_delivery(ctx: AccessContext, change: EmailChangeRequest) -> dict[str, object]:
    """An email change with the status of its two emails: to the new address, to the old one."""
    kind, ids = notification_selectors.EMAIL_CHANGE, [change.pk]
    confirm = notification_selectors.delivery_status(
        ctx, kind, ids, source_ref="email_change.confirm"
    )
    notice = notification_selectors.delivery_status(
        ctx, kind, ids, source_ref="email_change.cancel"
    )
    return {"change": change, "confirm": confirm.get(change.pk), "notice": notice.get(change.pk)}


# --- people ----------------------------------------------------------------------------


@require_safe
@require_capability(ORG)
def people(request: HttpRequest) -> HttpResponse:
    ctx = access_for(request)
    filters = PeopleFilterForm(request.GET or None)
    q = status = ""
    if filters.is_valid():
        q, status = filters.cleaned_data["q"], filters.cleaned_data["status"]
    page = Paginator(
        account_selectors.people(ctx, q=q, status=status), account_selectors.PAGE_SIZE
    ).get_page(request.GET.get("page"))
    template = (
        "organizations/people.html#results" if is_htmx(request) else "organizations/people.html"
    )
    return render(
        request,
        template,
        {"filters": filters, "page_obj": page, "extra_query": _extra_query(q=q, status=status)},
    )


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
        "is_self": user.pk == ctx.user_id,
        "memberships": selectors.memberships(ctx, user),
        "assignments": account_selectors.assignments_of(ctx, user),
        "invitations": _with_delivery(
            ctx, account_selectors.invitations(ctx, of=user, pending_only=False)[:10]
        ),
        "open_change": change,
        "change_delivery": None if change is None else _change_delivery(ctx, change),
        "membership_form": MembershipForm(ctx=ctx, actor_id=ctx.user_id),
        "employer_form": EmployerForm(initial={"company": user.employer_company_id}, ctx=ctx),
        "email_form": EmailChangeForm(),
        "confirm_form": ConfirmForm(),
    }
    return render(request, "organizations/person.html", context)


def _back(user_id: UUID) -> HttpResponse:
    return redirect("org:person", user_id=user_id)


@mutating(ORG)
def grant_membership(request: HttpRequest, user_id: UUID) -> HttpResponse:
    ctx = access_for(request)
    user = account_selectors.person(ctx, user_id)
    form = MembershipForm(request.POST, ctx=ctx)
    if not form.is_valid():
        messages.error(request, form_error_text(form))
        return _back(user.pk)
    try:
        services.grant_membership(ctx.principal, user_id=user.pk, **form.grant_kwargs())
    except ValidationError as exc:
        messages.error(request, error_text(exc))
    except IdempotencyError as exc:
        return idempotency_failure(request, exc)
    except RetryExhausted:
        return conflict(request)
    else:
        messages.success(request, _("Role granted."))
    return _back(user.pk)


@mutating(ORG)
def revoke_membership(request: HttpRequest, membership_id: UUID) -> HttpResponse:
    ctx = access_for(request)
    membership = selectors.membership(ctx, membership_id)
    try:
        services.revoke_membership(ctx.principal, membership.pk)
    except ValidationError as exc:
        messages.error(request, error_text(exc))
    except RetryExhausted:
        return conflict(request)
    else:
        messages.success(request, _("Role revoked."))
    return _back(membership.user_id)


@mutating(ORG)
def membership_validity(request: HttpRequest, membership_id: UUID) -> HttpResponse:
    ctx = access_for(request)
    membership = selectors.membership(ctx, membership_id)
    form = ValidityForm(request.POST)
    if not form.is_valid():
        messages.error(request, form_error_text(form))
        return _back(membership.user_id)
    try:
        services.change_membership_validity(
            ctx.principal,
            membership.pk,
            valid_from=form.cleaned_data["valid_from"],
            valid_until=form.cleaned_data["valid_until"],
        )
    except ValidationError as exc:
        messages.error(request, error_text(exc))
    except RetryExhausted:
        return conflict(request)
    else:
        messages.success(request, _("Validity saved."))
    return _back(membership.user_id)


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
    form = ConfirmForm(request.POST)
    if not form.is_valid():
        messages.error(request, _("Tick the confirmation to offboard this account."))
        return _back(user.pk)
    try:
        account_services.offboard_user(ctx.principal, user.pk)
    except ValidationError as exc:
        messages.error(request, error_text(exc))
    except RetryExhausted:
        return conflict(request)
    else:
        messages.success(request, _("Account offboarded."))
    return _back(user.pk)


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
    form = ReasonForm(request.POST)
    reason = form.cleaned_data["reason"] if form.is_valid() else ""
    try:
        account_services.revoke_email_change(ctx.principal, change.pk, reason=reason)
    except ValidationError as exc:
        messages.error(request, error_text(exc))
    except RetryExhausted:
        return conflict(request)
    else:
        messages.success(request, _("Email change revoked."))
    return _back(change.user_id)


# --- invitations -----------------------------------------------------------------------


@require_safe
@require_capability(ORG)
def invitations(request: HttpRequest) -> HttpResponse:
    ctx = access_for(request)
    filters = InvitationFilterForm(request.GET or None)
    q = filters.cleaned_data["q"] if filters.is_valid() else ""
    page = Paginator(account_selectors.invitations(ctx, q=q), account_selectors.PAGE_SIZE).get_page(
        request.GET.get("page")
    )
    page.object_list = _with_delivery(ctx, page.object_list)
    template = (
        "organizations/invitations.html#results"
        if is_htmx(request)
        else "organizations/invitations.html"
    )
    return render(
        request, template, {"filters": filters, "page_obj": page, "extra_query": _extra_query(q=q)}
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


@require_safe
@require_capability(ORG)
def invitation_delivery(request: HttpRequest, invitation_id: UUID) -> HttpResponse:
    """The badge of one invitation's email, polled while the message is in flight."""
    ctx = access_for(request)
    invitation = account_selectors.invitation(ctx, invitation_id)
    (row,) = _with_delivery(ctx, [invitation])
    return render(request, "organizations/delivery_invitation.html", {"invitation": row})


@require_safe
@require_capability(ORG)
def email_change_delivery(request: HttpRequest, change_id: UUID) -> HttpResponse:
    """The badges of the two emails of an email change, polled while one is in flight."""
    ctx = access_for(request)
    change = account_selectors.email_change(ctx, change_id)
    return render(
        request, "organizations/delivery_email_change.html", _change_delivery(ctx, change)
    )

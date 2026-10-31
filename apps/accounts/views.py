from typing import Any
from uuid import UUID

from allauth.account.views import PasswordResetFromKeyView as AllauthPasswordResetFromKeyView
from allauth.core.internal.ratelimit import consume_or_429
from django.contrib import messages
from django.http import HttpRequest, HttpResponse
from django.shortcuts import redirect, render
from django.utils.decorators import method_decorator
from django.utils.translation import gettext as _
from django.views.decorators.debug import sensitive_post_parameters
from django.views.decorators.http import require_POST, require_safe

from apps.access.exceptions import AccessDenied
from apps.accounts import selectors, services, tokens
from apps.accounts.adapters import PasswordResetTokenInvalid
from apps.accounts.forms import AcceptInvitationForm, ResetPasswordKeyForm
from apps.accounts.models import EmailChangeRequest, Invitation
from apps.accounts.tokens import hash_token
from apps.core.http import client_ip
from apps.core.locking import RetryExhausted
from apps.core.responses import conflict


@method_decorator(sensitive_post_parameters("password1", "password2"), name="dispatch")
class PasswordResetFromKeyView(AllauthPasswordResetFromKeyView):
    """allauth's reset view with the token re-validated under the user-row lock.

    The key never stays in the address bar: allauth stores it in the session and redirects
    to a key-less URL. A redemption that loses the race shows the invalid-link page.
    """

    form_class = ResetPasswordKeyForm

    def get_form_class(self) -> Any:
        return ResetPasswordKeyForm

    def form_valid(self, form: Any) -> HttpResponse:
        try:
            return super().form_valid(form)
        except PasswordResetTokenInvalid:
            response: HttpResponse = self.render_to_response(self.get_context_data(token_fail=True))
            return response


password_reset_from_key = PasswordResetFromKeyView.as_view()


# --- token pages (plan §5: invitations, email changes) ----------------------------------
#
# A token link (``/invite/<token>/``, ``/account/email-change/<token>/``) keeps only the
# token's hash in the session and redirects to a token-less page, so the token never stays
# in the address bar, the history or a ``Referer``. The page shows what the token allows;
# its form posts to a POST-only view that redeems the token as a ``TokenPrincipal``. Every
# expired, used, revoked or unknown token shows the same page. Links and submissions share
# a rate limit per client IP.

INVITATION_SESSION_KEY = "_gmat_invitation"
EMAIL_CHANGE_SESSION_KEY = "_gmat_email_change"
TOKEN_RATE_LIMITS = {"token": "20/m/ip"}


def _rate_limited(request: HttpRequest) -> HttpResponse | None:
    response: HttpResponse | None = consume_or_429(
        request, app="allauth.account", action="token", config=TOKEN_RATE_LIMITS, limit_get=True
    )
    return response


def _session_user_id(request: HttpRequest) -> UUID | None:
    return request.user.pk if request.user.is_authenticated else None


def _invalid(request: HttpRequest) -> HttpResponse:
    request.session.pop(INVITATION_SESSION_KEY, None)
    request.session.pop(EMAIL_CHANGE_SESSION_KEY, None)
    return render(request, "accounts/token_invalid.html")


def _done(request: HttpRequest, message: str) -> HttpResponse:
    messages.success(request, message)
    return redirect("core:home" if request.user.is_authenticated else "account_login")


@require_safe
def invitation_link(request: HttpRequest, token: str) -> HttpResponse:
    limited = _rate_limited(request)
    if limited is not None:
        return limited
    request.session[INVITATION_SESSION_KEY] = hash_token(token)
    return redirect("invitations:accept")


def _needs_password(invitation: Invitation) -> bool:
    target = invitation.target_user
    return target is None or not target.is_active or target.offboarded_at is not None


def _invitation_page(
    request: HttpRequest, invitation: Invitation, form: AcceptInvitationForm | None = None
) -> HttpResponse:
    problem = tokens.invitation_problem(
        invitation, invitation.target_user, _session_user_id(request)
    )
    if problem in {tokens.SIGN_IN_REQUIRED, tokens.SIGN_OUT_REQUIRED}:
        return render(
            request,
            "accounts/invitation_account.html",
            {"invitation": invitation, "sign_in": problem == tokens.SIGN_IN_REQUIRED},
        )
    if problem is not None:
        return _invalid(request)
    if form is None and _needs_password(invitation):
        form = AcceptInvitationForm(invitation=invitation)
    return render(
        request, "accounts/invitation_accept.html", {"invitation": invitation, "form": form}
    )


@require_safe
def invitation_accept(request: HttpRequest) -> HttpResponse:
    invitation = selectors.invitation_by_hash(request.session.get(INVITATION_SESSION_KEY))
    if invitation is None:
        return _invalid(request)
    return _invitation_page(request, invitation)


@require_POST
@sensitive_post_parameters("password1", "password2")
def invitation_submit(request: HttpRequest) -> HttpResponse:
    limited = _rate_limited(request)
    if limited is not None:
        return limited
    token_hash = request.session.get(INVITATION_SESSION_KEY)
    invitation = selectors.invitation_by_hash(token_hash)
    if invitation is None:
        return _invalid(request)
    password = None
    if _needs_password(invitation):
        form = AcceptInvitationForm(request.POST, invitation=invitation)
        if not form.is_valid():
            return _invitation_page(request, invitation, form)
        password = form.cleaned_data["password1"]
    try:
        principal = tokens.invitation_principal(
            str(token_hash), session_user_id=_session_user_id(request), ip=client_ip(request)
        )
        services.accept_invitation(principal, password=password)
    except AccessDenied, ValueError:
        return _invalid(request)
    except RetryExhausted:
        return conflict(request)
    request.session.pop(INVITATION_SESSION_KEY, None)
    if request.user.is_authenticated:
        return _done(request, _("Invitation accepted. Your new access is ready."))
    return _done(request, _("Your account is ready. Sign in to continue."))


@require_safe
def email_change_link(request: HttpRequest, token: str) -> HttpResponse:
    limited = _rate_limited(request)
    if limited is not None:
        return limited
    token_hash = hash_token(token)
    request.session[EMAIL_CHANGE_SESSION_KEY] = token_hash
    found = selectors.email_change_by_hash(token_hash)
    action = found[1] if found is not None else "confirm"
    return redirect(f"email_change:{action}")


def _email_change(
    request: HttpRequest, action: tokens.EmailChangeAction
) -> EmailChangeRequest | None:
    found = selectors.email_change_by_hash(request.session.get(EMAIL_CHANGE_SESSION_KEY))
    if found is None or found[1] != action or tokens.email_change_problem(found[0], action):
        return None
    return found[0]


def _email_change_page(request: HttpRequest, action: tokens.EmailChangeAction) -> HttpResponse:
    change = _email_change(request, action)
    if change is None:
        return _invalid(request)
    return render(
        request,
        "accounts/email_change.html",
        {
            "change": change,
            "action": action,
            "cooling_off_hours": int(tokens.EMAIL_CHANGE_COOLING_OFF.total_seconds() // 3600),
        },
    )


def _email_change_submit(request: HttpRequest, action: tokens.EmailChangeAction) -> HttpResponse:
    limited = _rate_limited(request)
    if limited is not None:
        return limited
    if _email_change(request, action) is None:
        return _invalid(request)
    try:
        principal = tokens.email_change_principal(
            str(request.session[EMAIL_CHANGE_SESSION_KEY]), action, ip=client_ip(request)
        )
        if action == "confirm":
            services.confirm_email_change(principal)
        else:
            services.cancel_email_change(principal)
    except AccessDenied:
        return _invalid(request)
    except RetryExhausted:
        return conflict(request)
    request.session.pop(EMAIL_CHANGE_SESSION_KEY, None)
    if action == "confirm":
        return _done(request, _("Address confirmed. The change applies after the waiting period."))
    return _done(request, _("The email change is cancelled."))


@require_safe
def email_change_confirm(request: HttpRequest) -> HttpResponse:
    return _email_change_page(request, "confirm")


@require_POST
def email_change_confirm_submit(request: HttpRequest) -> HttpResponse:
    return _email_change_submit(request, "confirm")


@require_safe
def email_change_cancel(request: HttpRequest) -> HttpResponse:
    return _email_change_page(request, "cancel")


@require_POST
def email_change_cancel_submit(request: HttpRequest) -> HttpResponse:
    return _email_change_submit(request, "cancel")

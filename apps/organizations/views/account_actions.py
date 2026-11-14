"""What a group administrator corrects on a person's account (M10.4, ADR-0030): the full name
and a lost second factor, each an inline dialog of the person's page posting here.

``org.manage`` with session MFA (``@mutating``). The services decide the rest under the user
row's lock: a staff or superuser account is refused (its factors only on the command line),
nobody resets their own factors, a reset needs its reason and the identity tick.
"""

from uuid import UUID

from django.http import HttpRequest, HttpResponse
from django.utils.translation import gettext as _

from apps.access.middleware import access_for
from apps.access.mixins import mutating
from apps.access.roles import Capability
from apps.accounts import selectors as account_selectors
from apps.accounts import services as account_services
from apps.core.responses import invalid, perform
from apps.organizations.forms import MfaResetForm, RenameForm
from apps.organizations.row_dialogs import mfa_reset_dialog, person_url, rename_dialog

ORG = Capability.ORG_MANAGE
RENAME_IDS = "id_rename_%s"
MFA_RESET_IDS = "id_mfa_reset_%s"


@mutating(ORG)
def rename_person(request: HttpRequest, user_id: UUID) -> HttpResponse:
    ctx = access_for(request)
    user = account_selectors.person(ctx, user_id)
    form = RenameForm(request.POST, auto_id=RENAME_IDS)
    dialog = rename_dialog(user, form).dialog()
    to = person_url(user.pk)
    if not form.is_valid():
        return invalid(request, form, to, dialog=dialog)
    return perform(
        request,
        lambda: account_services.rename_user(
            ctx.principal, user.pk, full_name=form.cleaned_data["full_name"]
        ),
        _("Name saved."),
        to,
        dialog=dialog,
    )


@mutating(ORG)
def reset_mfa(request: HttpRequest, user_id: UUID) -> HttpResponse:
    ctx = access_for(request)
    user = account_selectors.person(ctx, user_id)
    form = MfaResetForm(request.POST, auto_id=MFA_RESET_IDS)
    dialog = mfa_reset_dialog(user, form).dialog()
    to = person_url(user.pk)
    if not form.is_valid():
        return invalid(request, form, to, dialog=dialog)
    data = form.cleaned_data
    return perform(
        request,
        lambda: account_services.reset_user_mfa(
            ctx.principal,
            user.pk,
            reason=data["reason"],
            identity_checked=data["identity_checked"],
            idempotency_key=data["idempotency_key"],
        ),
        _("Second factors removed and sessions ended; a notice to the person is queued."),
        to,
        dialog=dialog,
    )

"""The attachment screens: upload, download and remove (plan §5; F73).

**Upload** follows Django's documented pattern for an upload handler that must run before
CSRF reads the body:

1. ``upload``, the outer view and the project's only ``csrf_exempt`` (allowlisted in
   ``scripts/check_access_rules.py``), refuses before reading any byte of the body anything
   that is not an authenticated session (cross-site requests carry no ``SameSite=Lax``
   session cookie), with the parent visible (404) and the right to add to it (403), a
   same-origin ``Origin`` header, a declared ``Content-Length`` within the limit (chunked
   bodies have none) and a multipart body;
2. it installs ``ReservingUploadHandler`` as the only upload handler: admission runs when the
   body starts to be parsed, and the file streams within its reservation;
3. it calls ``_receive``, decorated with ``csrf_protect``: the CSRF check parses the body
   through the handler, then the file is finalized. Whatever happens after admission, the
   reservation is released unless the file was stored.

Refusals answer with their status and a page that leads back to the document: 413 too
large, 411 no length, 429 busy (``Retry-After``), 507 storage full, 409 no slot left or the
document closed, 415 not an accepted file.

**Download** streams a stored file to whoever can view its parent, as an attachment, with
``nosniff`` and a ``sandbox`` CSP. **Remove** is one POST-only URL per attachment.
"""

import logging
from typing import TYPE_CHECKING
from uuid import UUID

from django.conf import settings
from django.contrib import messages
from django.contrib.auth.views import redirect_to_login
from django.core.exceptions import ValidationError
from django.http import FileResponse, Http404, HttpRequest, HttpResponse, HttpResponseBase
from django.shortcuts import redirect, render
from django.utils.translation import gettext as _
from django.utils.translation import gettext_lazy
from django.views.decorators.csrf import csrf_exempt, csrf_protect
from django.views.decorators.http import require_POST, require_safe

from apps.access.exceptions import AccessDenied
from apps.access.middleware import access_for
from apps.access.mixins import require
from apps.attachments import files, selectors, services
from apps.attachments.forms import RemoveForm
from apps.attachments.parents import ParentType, parent_of, parent_type, type_of
from apps.attachments.uploads import ReservingUploadHandler, UploadAborted
from apps.core.idempotency import IdempotencyError
from apps.core.locking import RetryExhausted
from apps.core.responses import conflict, idempotency_failure, perform

if TYPE_CHECKING:
    from django.utils.functional import _StrPromise as Message


logger = logging.getLogger(__name__)

STATUSES = {
    "origin": 403,
    "length": 411,
    "too_large": 413,
    "malformed": 400,
    "no_file": 400,
    "empty": 400,
    "busy": 429,
    "quota": 507,
    "slots": 409,
    "closed": 409,
    "expired": 409,
    "type": 415,
    "too_many_pixels": 415,
    "undecodable": 415,
    "timeout": 422,
}
MESSAGES: dict[str, Message] = {
    "origin": gettext_lazy("The upload did not come from this site."),
    "length": gettext_lazy("The upload did not say how large it is."),
    "malformed": gettext_lazy("Send one file with the form."),
    "no_file": gettext_lazy("Choose a file to attach."),
    **services.REFUSALS,
}
RETRY_AFTER = "30"


def _refused(request: HttpRequest, code: str, back: str) -> HttpResponse:
    code = code if code in STATUSES else "malformed"
    response = render(
        request,
        "attachments/refused.html",
        {"message": MESSAGES[code], "back": back},
        status=STATUSES[code],
    )
    if code == "busy":
        response["Retry-After"] = RETRY_AFTER
    return response


def _same_origin(request: HttpRequest) -> bool:
    origin = request.META.get("HTTP_ORIGIN", "")
    own = f"{'https' if request.is_secure() else 'http'}://{request.get_host()}"
    return bool(origin) and (origin == own or origin in settings.CSRF_TRUSTED_ORIGINS)


def _declared_length(request: HttpRequest) -> int | None:
    if "chunked" in request.META.get("HTTP_TRANSFER_ENCODING", "").lower():
        return None
    try:
        return int(request.META.get("CONTENT_LENGTH") or "")
    except ValueError:
        return None


def _before_body(request: HttpRequest) -> str | None:
    """The refusal code for what can be judged without reading the body, if any."""
    if not _same_origin(request):
        return "origin"
    length = _declared_length(request)
    if length is None:
        return "length"
    if length > settings.ATTACHMENT_MAX_REQUEST_BYTES:
        return "too_large"
    if request.content_type != "multipart/form-data":
        return "malformed"
    return None


@csrf_protect
def _receive(request: HttpRequest, *, handler: ReservingUploadHandler, back: str) -> HttpResponse:
    """The inner view: CSRF has parsed the body through ``handler``; finalize the file."""
    data = request.POST  # parses the body now if the CSRF check did not
    if handler.reservation_id is None or not handler.complete:
        return _refused(request, "no_file", back)
    reservation_id, handler.reservation_id = handler.reservation_id, None  # finalize owns it
    item = services.finalize_upload(
        access_for(request).principal,
        reservation_id,
        file_name=handler.sent_name,
        idempotency_key=data.get("idempotency_key") or None,
    )
    messages.success(request, _("%(name)s attached.") % {"name": item.file_name})
    return redirect(back)


def _release(request: HttpRequest, handler: ReservingUploadHandler) -> None:
    """Admitted but never finalized (CSRF refusal, broken or refused body): release now."""
    handler.close()
    if handler.reservation_id is None:
        return
    try:
        services.release_reservation(access_for(request).principal, handler.reservation_id)
    except Exception:  # the lease expiry releases it; the answer stays the original one
        logger.warning("Reservation %s left to its lease", handler.reservation_id, exc_info=True)
    files.discard_staged(handler.reservation_id)


def _upload(request: HttpRequest, ptype: ParentType, parent_id: UUID) -> HttpResponseBase:
    ctx = access_for(request)
    require(request, ptype.add)  # S7: the capability somewhere (403), then the parent (404, 403)
    found = selectors.parent(ctx, ptype, parent_id)
    require(request, ptype.add, found, read=ptype.view)
    back = ptype.page(found, request.GET.get("back", ""))
    code = _before_body(request)
    if code is not None:
        return _refused(request, code, back)
    handler = ReservingUploadHandler(
        request,
        admit=lambda declared: services.admit_upload(
            ctx.principal, ptype.kind, found.pk, declared_bytes=declared
        ),
    )
    request.upload_handlers = [handler]
    try:
        return _receive(request, handler=handler, back=back)
    except UploadAborted as exc:
        return _refused(request, exc.code, back)
    except ValidationError as exc:
        return _refused(request, str(getattr(exc, "code", "")), back)
    except IdempotencyError as exc:
        return idempotency_failure(request, exc)
    except RetryExhausted:
        return conflict(request)
    finally:
        _release(request, handler)


@csrf_exempt  # the one allowlisted entry point: admission comes before the body (F73)
@require_POST
def upload(request: HttpRequest, kind: str, parent_id: UUID) -> HttpResponseBase:
    """Attach one file to a request, a receipt or a handover (see the module docstring)."""
    ptype = parent_type(kind)  # from the URL pattern's fixed kwargs
    if not request.user.is_authenticated:
        return redirect_to_login(request.get_full_path())
    return _upload(request, ptype, parent_id)


@require_safe
def download(request: HttpRequest, attachment_id: UUID) -> HttpResponseBase:
    """The stored file, for whoever can view its document (404 otherwise)."""
    if not request.user.is_authenticated:
        return redirect_to_login(request.get_full_path())
    item = selectors.attachment(access_for(request), attachment_id)
    try:
        handle = files.stored_path(item.storage_key).open("rb")
    except FileNotFoundError:
        logger.error("Attachment %s has no file", item.pk)
        raise Http404("attachment") from None
    response = FileResponse(
        handle, as_attachment=True, filename=item.file_name, content_type=item.content_type
    )
    response["X-Content-Type-Options"] = "nosniff"
    response["Content-Security-Policy"] = "sandbox; default-src 'none'"
    return response


@require_POST
def remove(request: HttpRequest, attachment_id: UUID) -> HttpResponseBase:
    """Remove an attachment: the uploader on a draft, else a moderator with a reason."""
    if not request.user.is_authenticated:
        return redirect_to_login(request.get_full_path())
    ctx = access_for(request)
    item = selectors.attachment(ctx, attachment_id, removed=True)
    of = parent_of(item)
    if not selectors.can_remove(ctx, item):
        raise AccessDenied(type_of(of).moderate)
    to = type_of(of).page(of, request.GET.get("back", ""))
    form = RemoveForm(request.POST)
    reason = form.cleaned_data["reason"] if form.is_valid() else ""
    return perform(
        request,
        lambda: services.remove_attachment(ctx.principal, item.pk, reason=reason),
        _("%(name)s removed.") % {"name": item.file_name},
        to,
    )

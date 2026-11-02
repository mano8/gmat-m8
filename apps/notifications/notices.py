"""Business notifications: what each domain event mails, and to whom (plan §5, M7.1).

The subscribers run inside the transaction of the service that publishes the event
(``apps.core.events``), so a message exists exactly when the change commits and a rolled-back
change leaves none. Each one queues at most one message per request, with the recipients'
ids and a capability on the request (``request.review`` for reviewers, ``request.view``
for the requester) that the worker checks again at the claim.

- ``request.submitted``: the users who can review the request, except its author; its
  assigned reviewer (M7b.2) gets a message of their own instead;
- ``request.reviewer_assigned``: the new assignee, unless they assigned it to themselves;
- ``request.approved`` / ``request.rejected``: the requester, with their own decisions;
- ``request.edited``: the requester, when a reviewer edits their submitted or approved
  request (M7b.3), unless they edited it themselves. One message per batch of edits: it
  is held back ``EDIT_BATCH`` and says only that the request changed, so the edits made
  while it waits are covered by it (``outbox.waiting``) and none is queued for them;
- ``handover.posted``: the requester of each request served, with their own lines only
  (a ``DIRECT_RECEIPT`` handover is the receipt of goods on site).

Bodies are rendered in the installation's language and carry nothing beyond what the
recipient may already see on the request: no prices, suppliers, orders or other requests.
"""

from collections import defaultdict
from datetime import timedelta
from typing import Any

from django.conf import settings
from django.template.loader import render_to_string
from django.urls import reverse
from django.utils import translation
from django.utils.translation import gettext as _

from apps.access.roles import Capability
from apps.accounts.models import User
from apps.core.db import db_now
from apps.core.events import Event
from apps.handovers.models import Handover
from apps.material_requests import reviewers
from apps.material_requests.models import MaterialRequest
from apps.notifications import outbox, recipients
from apps.notifications.models import EmailOutbox
from apps.notifications.outbox import queue_notification

# How long the notice of a reviewer's edit waits for the edits that follow it.
EDIT_BATCH = timedelta(minutes=10)


def _absolute(path: str) -> str:
    return f"{str(settings.PUBLIC_BASE_URL).rstrip('/')}{path}"


def _queue(
    users: list[User],
    capability: Capability,
    request: MaterialRequest,
    template: str,
    subject: str,
    source_ref: str,
    context: dict[str, Any],
) -> list[EmailOutbox]:
    """Render ``template`` in the installation's language and queue it for ``users``."""
    if not users:
        return []
    with translation.override(settings.LANGUAGE_CODE):
        context = {"request_obj": request, **context}
        return queue_notification(
            recipients=[user.pk for user in users],
            capability=capability,
            obj=request,
            subject=f"{settings.ACCOUNT_EMAIL_SUBJECT_PREFIX}{subject}",
            body=render_to_string(f"notifications/email/{template}.txt", context),
            html_body=render_to_string(f"notifications/email/{template}.html", context),
            source_ref=source_ref,
        )


def _request(request_id: Any) -> MaterialRequest:
    return MaterialRequest.objects.select_related("project", "requested_by").get(pk=request_id)


def on_request_submitted(event: Event) -> None:
    request = _request(event.payload["request_id"])
    assigned = reviewers.assignee(request)
    with translation.override(settings.LANGUAGE_CODE):
        subject = _("Request %(number)s to review") % {"number": request.number}
        link = _absolute(reverse("requests:examine", args=[request.pk]))
    excluded = {request.requested_by_id} | ({assigned.pk} if assigned is not None else set())
    _queue(
        recipients.holders(Capability.REQUEST_REVIEW, request, exclude=excluded),
        Capability.REQUEST_REVIEW,
        request,
        "request_submitted",
        subject,
        f"request.submitted:{request.pk}",
        {"link": link, "lines": request.lines.count()},
    )
    if assigned is not None and assigned.pk != request.requested_by_id:
        _assigned(request, assigned, request.requested_by, "", f"request.submitted:{request.pk}")


def on_reviewer_assigned(event: Event) -> None:
    request = _request(event.payload["request_id"])
    reviewer = User.objects.get(pk=event.payload["reviewer_id"])
    if reviewer.pk == event.payload["assigned_by"]:
        return
    by = User.objects.get(pk=event.payload["assigned_by"])
    note = event.payload["note"]
    _assigned(request, reviewer, by, note, f"request.reviewer_assigned:{request.pk}")


def _assigned(
    request: MaterialRequest, reviewer: User, by: User, note: str, source_ref: str
) -> None:
    """Tell ``reviewer`` that ``by`` sent them ``request`` (while they may review it), with
    the note of a reassignment."""
    with translation.override(settings.LANGUAGE_CODE):
        subject = _("Request %(number)s assigned to you") % {"number": request.number}
        link = _absolute(reverse("requests:examine", args=[request.pk]))
    _queue(
        recipients.user_with(Capability.REQUEST_REVIEW, request, reviewer),
        Capability.REQUEST_REVIEW,
        request,
        "request_assigned",
        subject,
        source_ref,
        {"link": link, "lines": request.lines.count(), "assigned_by": by, "note": note},
    )


def on_request_decided(event: Event) -> None:
    request = _request(event.payload["request_id"])
    approved = event.name == "request.approved"
    with translation.override(settings.LANGUAGE_CODE):
        subject = (
            _("Request %(number)s approved") if approved else _("Request %(number)s rejected")
        ) % {"number": request.number}
        link = _absolute(reverse("requests:detail", args=[request.pk]))
    lines = request.lines.select_related("material", "unit").order_by("line_no")
    _queue(
        recipients.user_with(Capability.REQUEST_VIEW, request, request.requested_by),
        Capability.REQUEST_VIEW,
        request,
        "request_decided",
        subject,
        f"{event.name}:{request.pk}",
        {"link": link, "approved": approved, "lines": list(lines)},
    )


def on_request_edited(event: Event) -> None:
    request = _request(event.payload["request_id"])
    if event.payload["editor_id"] == request.requested_by_id:
        return
    source_ref = f"request.edited:{request.pk}"
    if outbox.waiting(source_ref):
        return  # the notice already queued covers this edit too
    with translation.override(settings.LANGUAGE_CODE):
        subject = _("Request %(number)s changed by a reviewer") % {"number": request.number}
        link = _absolute(reverse("requests:detail", args=[request.pk]))
    messages = _queue(
        recipients.user_with(Capability.REQUEST_VIEW, request, request.requested_by),
        Capability.REQUEST_VIEW,
        request,
        "request_edited",
        subject,
        source_ref,
        {"link": link},
    )
    outbox.defer(messages, db_now() + EDIT_BATCH)


def on_handover_posted(event: Event) -> None:
    handover = Handover.objects.select_related("project", "source_location").get(
        pk=event.payload["handover_id"]
    )
    by_request: defaultdict[Any, list[Any]] = defaultdict(list)
    for line in handover.lines.select_related(
        "request_line__request__project",
        "request_line__request__requested_by",
        "request_line__material",
        "request_line__unit",
    ).order_by("request_line__request__number", "request_line__line_no"):
        by_request[line.request_line.request].append(line)
    for request, lines in by_request.items():
        _handed_over(handover, request, lines)


def _handed_over(handover: Handover, request: MaterialRequest, lines: list[Any]) -> None:
    on_site = handover.kind == Handover.Kind.DIRECT_RECEIPT
    with translation.override(settings.LANGUAGE_CODE):
        subject = (
            _("Request %(number)s: goods received on site")
            if on_site
            else _("Request %(number)s: goods handed over")
        ) % {"number": request.number}
        link = _absolute(reverse("requests:detail", args=[request.pk]))
    _queue(
        recipients.user_with(Capability.REQUEST_VIEW, request, request.requested_by),
        Capability.REQUEST_VIEW,
        request,
        "request_handed_over",
        subject,
        f"handover.posted:{handover.pk}:{request.pk}",
        {
            "link": link,
            "handover": handover,
            "on_site": on_site,
            "lines": [(line.request_line, line.qty) for line in lines],
        },
    )


def register() -> None:
    """Subscribe the handlers (from ``AppConfig.ready``)."""
    from apps.core.events import subscribe

    subscribe("request.submitted", on_request_submitted)
    subscribe("request.reviewer_assigned", on_reviewer_assigned)
    subscribe("request.approved", on_request_decided)
    subscribe("request.rejected", on_request_decided)
    subscribe("request.edited", on_request_edited)
    subscribe("handover.posted", on_handover_posted)

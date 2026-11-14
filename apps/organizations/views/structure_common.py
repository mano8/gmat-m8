"""What the company and branch screens share (M10.2): sections, the double tap, the capability."""

from collections.abc import Callable
from typing import Any

from django import forms
from django.http import HttpRequest

from apps.access.middleware import access_for
from apps.access.roles import Capability
from apps.core.forms import IdempotentForm
from apps.core.idempotency import IdempotencyError, recorded_result
from apps.core.listing import ListPage, ListScope, paginate
from apps.organizations import selectors

ORG = Capability.ORG_MANAGE


def section(
    request: HttpRequest,
    rows: Callable[..., Any],
    scope: ListScope,
    form: type[forms.Form] | None = None,
) -> tuple[forms.Form | None, ListPage]:
    """A paged section of a page: ``rows(**filters)`` paged by ``scope``, its filter fields
    named after the section (``branches-state``)."""
    filters = None
    data: dict[str, Any] = {}
    if form is not None:
        filters = form(request.GET or None, prefix=scope.prefix.removesuffix("_"))
        data = filters.cleaned_data if filters.is_valid() else {}
    listing = paginate(
        request, rows(**data), default_per_page=selectors.SECTION_PER_PAGE, scope=scope
    )
    return filters, listing


def already_done(request: HttpRequest, form: IdempotentForm) -> bool:
    """The form's key already produced something: a second tap whose form the first one
    made invalid (a member no longer offered) leads where the first one did."""
    key = form.data.get(form.add_prefix("idempotency_key"), "")
    if not key:
        return False
    try:
        found = recorded_result(
            str(key), actor_id=access_for(request).principal.user_id, operation=form.operation
        )
    except IdempotencyError:
        return False
    return found is not None

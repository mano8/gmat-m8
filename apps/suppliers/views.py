"""Supplier screens (plan §4.2, M3.2): list, detail, create, edit, deactivate.

Reads go through ``apps.suppliers.selectors`` (``supplier.view``: group administrators,
material managers, warehouse operators); writes through ``apps.suppliers.services`` from
POST-only views (``@mutating``), each on its own URL, for ``supplier.manage``. A user
without ``supplier.view`` gets 403 on every URL here (S7), and the services authorize again.
"""

from urllib.parse import urlencode
from uuid import UUID

from django.contrib import messages
from django.core.exceptions import ValidationError
from django.core.paginator import Paginator
from django.http import HttpRequest, HttpResponse
from django.shortcuts import redirect, render
from django.utils.translation import gettext as _
from django.views.decorators.http import require_safe

from apps.access.middleware import access_for
from apps.access.mixins import mutating, require, require_capability
from apps.core.forms import add_service_errors, error_text
from apps.core.http import is_htmx
from apps.core.idempotency import IdempotencyError
from apps.core.locking import RetryExhausted
from apps.core.responses import conflict, idempotency_failure
from apps.suppliers import selectors, services
from apps.suppliers.forms import SupplierCreateForm, SupplierFilterForm, SupplierForm
from apps.suppliers.models import Supplier
from apps.suppliers.selectors import MANAGE, VIEW


def _manageable(request: HttpRequest, supplier_id: UUID) -> Supplier:
    """The supplier, if visible (404) and manageable (403) by the request's user."""
    supplier = selectors.supplier(access_for(request), supplier_id)
    require(request, MANAGE, supplier, read=VIEW)
    return supplier


def _kept(**params: str | None) -> dict[str, str]:
    """The filters that are set, for the pager's links."""
    return {key: value for key, value in params.items() if value}


@require_safe
@require_capability(VIEW)
def supplier_list(request: HttpRequest) -> HttpResponse:
    ctx = access_for(request)
    filters = SupplierFilterForm(request.GET or None)
    data = filters.cleaned_data if filters.is_valid() else {}
    q = data.get("q", "")
    state = data.get("state") or selectors.STATE_ACTIVE
    page = Paginator(selectors.suppliers(ctx, q=q, state=state), selectors.PAGE_SIZE).get_page(
        request.GET.get("page")
    )
    kept = _kept(q=q, state=request.GET.get("state"))
    template = "suppliers/list.html#results" if is_htmx(request) else "suppliers/list.html"
    context = {
        "filters": filters,
        "page_obj": page,
        "extra_query": f"&{urlencode(kept)}" if kept else "",
        "manageable": selectors.manageable(ctx),
    }
    return render(request, template, context)


@require_safe
@require_capability(VIEW)
def supplier_detail(request: HttpRequest, supplier_id: UUID) -> HttpResponse:
    ctx = access_for(request)
    supplier = selectors.supplier(ctx, supplier_id)
    context = {"supplier": supplier, "manageable": selectors.manageable(ctx, supplier)}
    return render(request, "suppliers/detail.html", context)


@require_safe
@require_capability(MANAGE)
def supplier_new(request: HttpRequest) -> HttpResponse:
    form = SupplierCreateForm(actor_id=access_for(request).user_id)
    return render(request, "suppliers/form.html", {"form": form, "supplier": None})


@mutating(MANAGE)
def supplier_create(request: HttpRequest) -> HttpResponse:
    ctx = access_for(request)
    form = SupplierCreateForm(request.POST)
    if form.is_valid():
        try:
            supplier = services.create_supplier(ctx.principal, **form.create_kwargs())
        except ValidationError as exc:
            add_service_errors(form, exc)
        except IdempotencyError as exc:
            return idempotency_failure(request, exc)
        except RetryExhausted:
            return conflict(request)
        else:
            messages.success(request, _("Supplier %(name)s added.") % {"name": supplier.name})
            return redirect("suppliers:detail", supplier_id=supplier.pk)
    return render(request, "suppliers/form.html", {"form": form, "supplier": None})


@require_safe
@require_capability(MANAGE)
def supplier_edit(request: HttpRequest, supplier_id: UUID) -> HttpResponse:
    supplier = _manageable(request, supplier_id)
    form = SupplierForm(initial=SupplierForm.initial_for(supplier))
    return render(request, "suppliers/form.html", {"form": form, "supplier": supplier})


@mutating(MANAGE)
def supplier_update(request: HttpRequest, supplier_id: UUID) -> HttpResponse:
    ctx = access_for(request)
    supplier = _manageable(request, supplier_id)
    form = SupplierForm(request.POST)
    if form.is_valid():
        try:
            services.update_supplier(ctx.principal, supplier.pk, **form.values())
        except ValidationError as exc:
            add_service_errors(form, exc)
        except RetryExhausted:
            return conflict(request)
        else:
            messages.success(request, _("Supplier %(name)s saved.") % {"name": supplier.name})
            return redirect("suppliers:detail", supplier_id=supplier.pk)
    return render(request, "suppliers/form.html", {"form": form, "supplier": supplier})


def _set_active(request: HttpRequest, supplier_id: UUID, *, active: bool) -> HttpResponse:
    ctx = access_for(request)
    supplier = _manageable(request, supplier_id)
    action = services.reactivate_supplier if active else services.deactivate_supplier
    try:
        action(ctx.principal, supplier.pk)
    except ValidationError as exc:
        messages.error(request, error_text(exc))
    except RetryExhausted:
        return conflict(request)
    else:
        messages.success(
            request,
            _("Supplier %(name)s reactivated.") % {"name": supplier.name}
            if active
            else _("Supplier %(name)s deactivated.") % {"name": supplier.name},
        )
    return redirect("suppliers:detail", supplier_id=supplier.pk)


@mutating(MANAGE)
def supplier_deactivate(request: HttpRequest, supplier_id: UUID) -> HttpResponse:
    return _set_active(request, supplier_id, active=False)


@mutating(MANAGE)
def supplier_reactivate(request: HttpRequest, supplier_id: UUID) -> HttpResponse:
    return _set_active(request, supplier_id, active=True)

"""Supplier screens (plan §4.2, M3.2): list, detail, create, edit, deactivate.

Reads go through ``apps.suppliers.selectors`` (``supplier.view``: group administrators,
material managers, warehouse operators); writes through ``apps.suppliers.services`` from
POST-only views (``@mutating``), each on its own URL, for ``supplier.manage``. A user
without ``supplier.view`` gets 403 on every URL here (S7), and the services authorize again.
"""

from uuid import UUID

from django.contrib import messages
from django.core.exceptions import ValidationError
from django.forms import Form
from django.http import HttpRequest, HttpResponse
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils.translation import gettext as _
from django.views.decorators.http import require_safe

from apps.access.middleware import access_for
from apps.access.mixins import mutating, require, require_capability
from apps.core.dialogs import ActionDialog
from apps.core.forms import add_service_errors
from apps.core.idempotency import IdempotencyError
from apps.core.listing import paginate, render_list
from apps.core.locking import RetryExhausted
from apps.core.responses import conflict, idempotency_failure, perform
from apps.suppliers import selectors, services
from apps.suppliers.forms import SupplierCreateForm, SupplierFilterForm, SupplierForm
from apps.suppliers.models import Supplier
from apps.suppliers.selectors import MANAGE, VIEW


def _manageable(request: HttpRequest, supplier_id: UUID) -> Supplier:
    """The supplier, if visible (404) and manageable (403) by the request's user."""
    supplier = selectors.supplier(access_for(request), supplier_id)
    require(request, MANAGE, supplier, read=VIEW)
    return supplier


@require_safe
@require_capability(VIEW)
def supplier_list(request: HttpRequest) -> HttpResponse:
    ctx = access_for(request)
    filters = SupplierFilterForm(request.GET or None)
    data = filters.cleaned_data if filters.is_valid() else {}
    q = data.get("q", "")
    state = data.get("state") or selectors.STATE_ACTIVE
    listing = paginate(
        request,
        selectors.suppliers(ctx, q=q, state=state),
        default_per_page=selectors.DEFAULT_PER_PAGE,
        sorts=selectors.SUPPLIER_SORTS,
        default_sort="name",
        sort_labels=selectors.SUPPLIER_SORT_LABELS,
    )
    context = {"filters": filters, "listing": listing, "manageable": selectors.manageable(ctx)}
    return render_list(request, "suppliers/list.html", context)


@require_safe
@require_capability(VIEW)
def supplier_detail(request: HttpRequest, supplier_id: UUID) -> HttpResponse:
    ctx = access_for(request)
    supplier = selectors.supplier(ctx, supplier_id)
    manageable = selectors.manageable(ctx, supplier)
    context = {
        "supplier": supplier,
        "manageable": manageable,
        "deactivate_dialog": deactivate_dialog(supplier, Form()) if manageable else None,
    }
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


def deactivate_dialog(supplier: Supplier, form: Form) -> ActionDialog:
    """Deactivating a supplier, as a confirmation dialog of its page (M8b.4)."""
    return ActionDialog(
        id="deactivate-dialog",
        title=_("Deactivate the supplier"),
        description=supplier.name,
        icon="ban",
        tone="danger",
        action=reverse("suppliers:deactivate", args=[supplier.pk]),
        submit=_("Deactivate"),
        form=form,
        consequences=(
            _("The supplier is no longer offered for new orders."),
            _("Existing orders and documents keep it. It can be reactivated."),
        ),
        cancel=_("Keep it active"),
        testid="deactivate-form",
        submit_testid="deactivate-submit",
    )


def _set_active(request: HttpRequest, supplier_id: UUID, *, active: bool) -> HttpResponse:
    ctx = access_for(request)
    supplier = _manageable(request, supplier_id)
    action = services.reactivate_supplier if active else services.deactivate_supplier
    form = Form(request.POST)
    form.is_valid()  # no fields: a service refusal can then be shown on it
    return perform(
        request,
        lambda: action(ctx.principal, supplier.pk),
        _("Supplier %(name)s reactivated.") % {"name": supplier.name}
        if active
        else _("Supplier %(name)s deactivated.") % {"name": supplier.name},
        reverse("suppliers:detail", args=[supplier.pk]),
        dialog=None
        if active
        else deactivate_dialog(supplier, form).dialog(),  # only that one is a dialog
    )


@mutating(MANAGE)
def supplier_deactivate(request: HttpRequest, supplier_id: UUID) -> HttpResponse:
    return _set_active(request, supplier_id, active=False)


@mutating(MANAGE)
def supplier_reactivate(request: HttpRequest, supplier_id: UUID) -> HttpResponse:
    return _set_active(request, supplier_id, active=True)

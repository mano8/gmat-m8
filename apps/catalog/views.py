"""Catalog screens (plan §4.2, M3.1): list, search, detail, create, edit and categories.

Reads go through ``apps.catalog.selectors``; writes through ``apps.catalog.services`` from
POST-only views (``@mutating``), each on its own URL. The catalog is shared by the whole
installation, so the order of S7's checks collapses to: the capability somewhere (403),
then the object (404 when the user cannot see the catalog at all, 403 when they may not
change it). The services authorize again inside their transaction.
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
from apps.access.roles import Capability
from apps.catalog import selectors, services
from apps.catalog.forms import (
    CategoryCreateForm,
    CategoryForm,
    MaterialCreateForm,
    MaterialFilterForm,
    MaterialForm,
)
from apps.catalog.models import Material, MaterialCategory
from apps.core.dialogs import ActionDialog
from apps.core.forms import add_service_errors
from apps.core.idempotency import IdempotencyError
from apps.core.listing import paginate, render_list
from apps.core.locking import RetryExhausted
from apps.core.responses import conflict, idempotency_failure, perform

VIEW, MANAGE = Capability.CATALOG_VIEW, Capability.CATALOG_MANAGE


def _manageable(request: HttpRequest, material_id: UUID) -> Material:
    """The material, if visible (404) and manageable (403) by the request's user."""
    material = selectors.material(access_for(request), material_id)
    require(request, MANAGE, material, read=VIEW)
    return material


# --- materials ----------------------------------------------------------------------


@require_safe
@require_capability(VIEW)
def material_list(request: HttpRequest) -> HttpResponse:
    ctx = access_for(request)
    filters = MaterialFilterForm(request.GET or None, ctx=ctx)
    q, category, state = "", None, selectors.STATE_ACTIVE
    if filters.is_valid():
        q = filters.cleaned_data["q"]
        category = filters.cleaned_data["category"]
        state = filters.cleaned_data["state"] or selectors.STATE_ACTIVE
    listing = paginate(
        request,
        selectors.materials(ctx, q=q, category_id=category.pk if category else None, state=state),
        default_per_page=selectors.DEFAULT_PER_PAGE,
        sorts=selectors.MATERIAL_SORTS,
        default_sort="reference",
        sort_labels=selectors.MATERIAL_SORT_LABELS,
    )
    context = {"filters": filters, "listing": listing, "manageable": selectors.manageable(ctx)}
    return render_list(request, "catalog/list.html", context)


@require_safe
@require_capability(VIEW)
def material_search(request: HttpRequest) -> HttpResponse:
    """The best 20 active materials for ``?q=``, as a fragment for a picker (never prices)."""
    ctx = access_for(request)
    query = request.GET.get("q", "")
    return render(
        request,
        "catalog/search_results.html",
        {"materials": selectors.search(ctx, query), "query": query.strip()},
    )


@require_safe
@require_capability(VIEW)
def material_detail(request: HttpRequest, material_id: UUID) -> HttpResponse:
    ctx = access_for(request)
    material = selectors.material(ctx, material_id)
    manageable = selectors.manageable(ctx, material)
    context = {
        "material": material,
        "manageable": manageable,
        "deactivate_dialog": deactivate_dialog(material, Form()) if manageable else None,
    }
    return render(request, "catalog/detail.html", context)


@require_safe
@require_capability(MANAGE)
def material_new(request: HttpRequest) -> HttpResponse:
    ctx = access_for(request)
    form = MaterialCreateForm(ctx=ctx, actor_id=ctx.user_id)
    return render(request, "catalog/form.html", {"form": form, "material": None})


@mutating(MANAGE)
def material_create(request: HttpRequest) -> HttpResponse:
    ctx = access_for(request)
    form = MaterialCreateForm(request.POST, ctx=ctx)
    if form.is_valid():
        try:
            material = services.create_material(ctx.principal, **form.create_kwargs())
        except ValidationError as exc:
            add_service_errors(form, exc)
        except IdempotencyError as exc:
            return idempotency_failure(request, exc)
        except RetryExhausted:
            return conflict(request)
        else:
            messages.success(
                request, _("Material %(reference)s added.") % {"reference": material.reference}
            )
            return redirect("catalog:detail", material_id=material.pk)
    return render(request, "catalog/form.html", {"form": form, "material": None})


@require_safe
@require_capability(MANAGE)
def material_edit(request: HttpRequest, material_id: UUID) -> HttpResponse:
    ctx = access_for(request)
    material = _manageable(request, material_id)
    form = MaterialForm(initial=MaterialForm.initial_for(material), ctx=ctx)
    return render(request, "catalog/form.html", {"form": form, "material": material})


@mutating(MANAGE)
def material_update(request: HttpRequest, material_id: UUID) -> HttpResponse:
    ctx = access_for(request)
    material = _manageable(request, material_id)
    form = MaterialForm(request.POST, ctx=ctx)
    if form.is_valid():
        try:
            services.update_material(ctx.principal, material.pk, **form.values())
        except ValidationError as exc:
            add_service_errors(form, exc)
        except RetryExhausted:
            return conflict(request)
        else:
            messages.success(
                request, _("Material %(reference)s saved.") % {"reference": material.reference}
            )
            return redirect("catalog:detail", material_id=material.pk)
    return render(request, "catalog/form.html", {"form": form, "material": material})


def deactivate_dialog(material: Material, form: Form) -> ActionDialog:
    """Deactivating a material, as a confirmation dialog of its page (M8b.4)."""
    return ActionDialog(
        id="deactivate-dialog",
        title=_("Deactivate the material"),
        description=f"{material.reference} · {material.name}",
        icon="ban",
        tone="danger",
        action=reverse("catalog:deactivate", args=[material.pk]),
        submit=_("Deactivate"),
        form=form,
        consequences=(
            _("The material is hidden from searches and new documents."),
            _("Existing documents keep it. It can be reactivated."),
        ),
        cancel=_("Keep it active"),
        testid="deactivate-form",
        submit_testid="deactivate-submit",
    )


def _set_active(request: HttpRequest, material_id: UUID, *, active: bool) -> HttpResponse:
    ctx = access_for(request)
    material = _manageable(request, material_id)
    action = services.reactivate_material if active else services.deactivate_material
    form = Form(request.POST)
    form.is_valid()  # no fields: a service refusal can then be shown on it
    return perform(
        request,
        lambda: action(ctx.principal, material.pk),
        _("Material %(reference)s reactivated.") % {"reference": material.reference}
        if active
        else _("Material %(reference)s deactivated.") % {"reference": material.reference},
        reverse("catalog:detail", args=[material.pk]),
        dialog=None
        if active
        else deactivate_dialog(material, form).dialog(),  # only that one is a dialog
    )


@mutating(MANAGE)
def material_deactivate(request: HttpRequest, material_id: UUID) -> HttpResponse:
    return _set_active(request, material_id, active=False)


@mutating(MANAGE)
def material_reactivate(request: HttpRequest, material_id: UUID) -> HttpResponse:
    return _set_active(request, material_id, active=True)


# --- categories ---------------------------------------------------------------------


@require_safe
@require_capability(VIEW)
def category_list(request: HttpRequest) -> HttpResponse:
    ctx = access_for(request)
    context = {"categories": selectors.categories(ctx), "manageable": selectors.manageable(ctx)}
    return render(request, "catalog/category_list.html", context)


@require_safe
@require_capability(MANAGE)
def category_new(request: HttpRequest) -> HttpResponse:
    ctx = access_for(request)
    form = CategoryCreateForm(ctx=ctx, actor_id=ctx.user_id)
    return render(request, "catalog/category_form.html", {"form": form, "category": None})


@mutating(MANAGE)
def category_create(request: HttpRequest) -> HttpResponse:
    ctx = access_for(request)
    form = CategoryCreateForm(request.POST, ctx=ctx)
    if form.is_valid():
        try:
            category = services.create_category(ctx.principal, **form.create_kwargs())
        except ValidationError as exc:
            add_service_errors(form, exc)
        except IdempotencyError as exc:
            return idempotency_failure(request, exc)
        except RetryExhausted:
            return conflict(request)
        else:
            messages.success(request, _("Category %(name)s added.") % {"name": category.name})
            return redirect("catalog:categories")
    return render(request, "catalog/category_form.html", {"form": form, "category": None})


def _manageable_category(request: HttpRequest, category_id: UUID) -> MaterialCategory:
    category = selectors.category(access_for(request), category_id)
    require(request, MANAGE, category, read=VIEW)
    return category


@require_safe
@require_capability(MANAGE)
def category_edit(request: HttpRequest, category_id: UUID) -> HttpResponse:
    ctx = access_for(request)
    category = _manageable_category(request, category_id)
    form = CategoryForm(initial=CategoryForm.initial_for(category), ctx=ctx, editing=category)
    return render(request, "catalog/category_form.html", {"form": form, "category": category})


@mutating(MANAGE)
def category_update(request: HttpRequest, category_id: UUID) -> HttpResponse:
    ctx = access_for(request)
    category = _manageable_category(request, category_id)
    form = CategoryForm(request.POST, ctx=ctx, editing=category)
    if form.is_valid():
        try:
            services.update_category(ctx.principal, category.pk, **form.values())
        except ValidationError as exc:
            add_service_errors(form, exc)
        except RetryExhausted:
            return conflict(request)
        else:
            messages.success(request, _("Category %(name)s saved.") % {"name": category.name})
            return redirect("catalog:categories")
    return render(request, "catalog/category_form.html", {"form": form, "category": category})

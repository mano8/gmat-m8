from django.urls import path

from apps.purchasing import views

app_name = "purchasing"

urlpatterns = [
    path("", views.worklist, name="worklist"),
    path("new/", views.order_new, name="new"),
    path("create/", views.order_create, name="create"),
    path("orders/", views.order_list, name="orders"),
    path("orders/<uuid:po_id>/", views.order_detail, name="detail"),
    path("orders/<uuid:po_id>/print/", views.order_print, name="print"),
    path("orders/<uuid:po_id>/amendment/", views.order_amendment, name="amendment"),
    path("orders/<uuid:po_id>/update/", views.order_update, name="update"),
    path("orders/<uuid:po_id>/lines/add/", views.lines_add, name="add_lines"),
    path("orders/<uuid:po_id>/issue/", views.order_issue, name="issue"),
    path("orders/<uuid:po_id>/cancel/", views.order_cancel, name="cancel"),
    path("orders/<uuid:po_id>/amend/", views.order_amend, name="amend"),
    path("lines/<uuid:line_id>/update/", views.line_update, name="update_line"),
    path("lines/<uuid:line_id>/remove/", views.line_remove, name="remove_line"),
    path("lines/<uuid:line_id>/close-short/", views.line_close_short, name="close_line"),
    path(
        "allocations/<uuid:allocation_id>/increase/",
        views.allocation_increase,
        name="increase_allocation",
    ),
    path("dispatches/<uuid:dispatch_id>/confirm/", views.dispatch_confirm, name="confirm_dispatch"),
    path("dispatches/<uuid:dispatch_id>/resend/", views.dispatch_resend, name="resend_dispatch"),
    path(
        "allocations/<uuid:allocation_id>/unallocate/",
        views.allocation_unallocate,
        name="unallocate",
    ),
]

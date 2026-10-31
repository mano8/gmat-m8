from django.urls import path

from apps.suppliers import views

app_name = "suppliers"

urlpatterns = [
    path("", views.supplier_list, name="list"),
    path("new/", views.supplier_new, name="new"),
    path("create/", views.supplier_create, name="create"),
    path("<uuid:supplier_id>/", views.supplier_detail, name="detail"),
    path("<uuid:supplier_id>/edit/", views.supplier_edit, name="edit"),
    path("<uuid:supplier_id>/update/", views.supplier_update, name="update"),
    path("<uuid:supplier_id>/deactivate/", views.supplier_deactivate, name="deactivate"),
    path("<uuid:supplier_id>/reactivate/", views.supplier_reactivate, name="reactivate"),
]

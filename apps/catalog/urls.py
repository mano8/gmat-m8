from django.urls import path

from apps.catalog import unit_views, views

app_name = "catalog"

urlpatterns = [
    path("", views.material_list, name="list"),
    path("search/", views.material_search, name="search"),
    path("new/", views.material_new, name="new"),
    path("create/", views.material_create, name="create"),
    path("<uuid:material_id>/", views.material_detail, name="detail"),
    path("<uuid:material_id>/edit/", views.material_edit, name="edit"),
    path("<uuid:material_id>/update/", views.material_update, name="update"),
    path("<uuid:material_id>/deactivate/", views.material_deactivate, name="deactivate"),
    path("<uuid:material_id>/reactivate/", views.material_reactivate, name="reactivate"),
    path("units/", unit_views.units, name="units"),
    path("units/new/", unit_views.new_unit, name="new_unit"),
    path("units/create/", unit_views.create_unit, name="create_unit"),
    path("units/<uuid:unit_id>/edit/", unit_views.edit_unit, name="edit_unit"),
    path("units/<uuid:unit_id>/update/", unit_views.update_unit, name="update_unit"),
    path("units/<uuid:unit_id>/deactivate/", unit_views.deactivate_unit, name="deactivate_unit"),
    path("units/<uuid:unit_id>/reactivate/", unit_views.reactivate_unit, name="reactivate_unit"),
    path("categories/", views.category_list, name="categories"),
    path("categories/new/", views.category_new, name="category_new"),
    path("categories/create/", views.category_create, name="category_create"),
    path("categories/<uuid:category_id>/edit/", views.category_edit, name="category_edit"),
    path("categories/<uuid:category_id>/update/", views.category_update, name="category_update"),
]

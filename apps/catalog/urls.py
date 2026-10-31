from django.urls import path

from apps.catalog import views

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
    path("categories/", views.category_list, name="categories"),
    path("categories/new/", views.category_new, name="category_new"),
    path("categories/create/", views.category_create, name="category_create"),
    path("categories/<uuid:category_id>/edit/", views.category_edit, name="category_edit"),
    path("categories/<uuid:category_id>/update/", views.category_update, name="category_update"),
]

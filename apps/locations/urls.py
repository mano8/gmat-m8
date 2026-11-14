from django.urls import path

from apps.locations import views

app_name = "locations"

urlpatterns = [
    path("warehouses/", views.warehouses, name="warehouses"),
    path("warehouses/new/", views.new_warehouse, name="new_warehouse"),
    path("warehouses/create/", views.create_warehouse, name="create_warehouse"),
    path("warehouses/<uuid:location_id>/edit/", views.edit_warehouse, name="edit_warehouse"),
    path("warehouses/<uuid:location_id>/update/", views.update_warehouse, name="update_warehouse"),
    path(
        "warehouses/<uuid:location_id>/deactivate/confirm/",
        views.confirm_deactivate_warehouse,
        name="confirm_deactivate_warehouse",
    ),
    path(
        "warehouses/<uuid:location_id>/deactivate/",
        views.deactivate_warehouse,
        name="deactivate_warehouse",
    ),
    path(
        "warehouses/<uuid:location_id>/reactivate/",
        views.reactivate_warehouse,
        name="reactivate_warehouse",
    ),
    path("worksites/<uuid:location_id>/update/", views.update_worksite, name="update_worksite"),
    path(
        "worksites/<uuid:location_id>/deactivate/",
        views.deactivate_worksite,
        name="deactivate_worksite",
    ),
    path(
        "worksites/<uuid:location_id>/reactivate/",
        views.reactivate_worksite,
        name="reactivate_worksite",
    ),
]

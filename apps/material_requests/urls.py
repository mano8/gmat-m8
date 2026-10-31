from django.urls import path

from apps.material_requests import views

app_name = "requests"

urlpatterns = [
    path("", views.request_list, name="list"),
    path("new/", views.request_new, name="new"),
    path("start/", views.request_start, name="start"),
    path("<uuid:request_id>/", views.request_detail, name="detail"),
    path("<uuid:request_id>/materials/", views.request_materials, name="materials"),
    path("<uuid:request_id>/review/", views.request_review, name="review"),
    path("<uuid:request_id>/lines/add/", views.line_add, name="add_line"),
    path("<uuid:request_id>/lines/add-free/", views.line_add_free, name="add_free_line"),
    path("<uuid:request_id>/submit/", views.request_submit, name="submit"),
    path("<uuid:request_id>/withdraw/", views.request_withdraw, name="withdraw"),
    path("<uuid:request_id>/cancel/", views.request_cancel, name="cancel"),
    path("lines/<uuid:line_id>/update/", views.line_update, name="update_line"),
    path("lines/<uuid:line_id>/remove/", views.line_remove, name="remove_line"),
]

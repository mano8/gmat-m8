from django.urls import path

from apps.projects import views

app_name = "projects"

urlpatterns = [
    path("", views.project_list, name="list"),
    path("new/", views.project_new, name="new"),
    path("create/", views.project_create, name="create"),
    path("<uuid:project_id>/", views.project_detail, name="detail"),
    path("<uuid:project_id>/edit/", views.project_edit, name="edit"),
    path("<uuid:project_id>/update/", views.project_update, name="update"),
    path("<uuid:project_id>/assignments/create/", views.assignment_create, name="assign"),
    path(
        "assignments/<uuid:assignment_id>/revoke/",
        views.assignment_revoke,
        name="revoke_assignment",
    ),
    path(
        "assignments/<uuid:assignment_id>/validity/",
        views.assignment_validity,
        name="assignment_validity",
    ),
]

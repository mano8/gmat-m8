from django.urls import path

from apps.audit import views

app_name = "audit"

urlpatterns = [
    path("", views.event_list, name="list"),
    path("object/<str:object_type>/<uuid:object_id>/", views.object_history, name="object"),
]

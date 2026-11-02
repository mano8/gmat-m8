from django.urls import path

from apps.handovers import views

app_name = "handovers"

urlpatterns = [
    path("", views.handover_list, name="list"),
    path("new/", views.handover_new, name="new"),
    path("create/", views.handover_create, name="create"),
    path("<uuid:handover_id>/", views.handover_detail, name="detail"),
    path("<uuid:handover_id>/void/", views.handover_void, name="void"),
]

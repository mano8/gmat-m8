from django.urls import path

from apps.dashboards import views

app_name = "dashboard"

urlpatterns = [
    path("panels/<slug:key>/", views.panel, name="panel"),
]

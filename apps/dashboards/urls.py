from django.urls import path

from apps.dashboards import views

app_name = "dashboard"

urlpatterns = [
    path("tiles/", views.key_figures, name="tiles"),
    path("panels/<slug:key>/", views.panel, name="panel"),
]

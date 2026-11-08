from django.urls import path

from apps.core import views
from apps.dashboards import views as dashboards

app_name = "core"

urlpatterns = [
    path("", dashboards.home, name="home"),
    path("ui/preferences/", views.ui_preferences, name="ui_preferences"),
    path("healthz/", views.healthz, name="healthz"),
    path("readyz/", views.readyz, name="readyz"),
]

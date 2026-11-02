from django.urls import path

from apps.notifications import views

app_name = "notifications"

urlpatterns = [
    path("", views.settings_page, name="settings"),
    path("save/", views.settings_save, name="save"),
]

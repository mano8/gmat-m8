from django.urls import path

from apps.notifications import views

app_name = "notifications"

urlpatterns = [
    path("", views.settings_page, name="settings"),
    path("channels/", views.save_channels, name="save_channels"),
    path("choices/<slug:channel>/", views.save_choices, name="save_choices"),
]

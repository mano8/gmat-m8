from django.urls import path

from apps.notifications import inbox_views

app_name = "inapp"

urlpatterns = [
    path("", inbox_views.inbox, name="list"),
    path("bell/", inbox_views.bell, name="bell"),
    path("read-all/", inbox_views.mark_all_read, name="read_all"),
    path("<uuid:notification_id>/open/", inbox_views.open_notification, name="open"),
]

from django.apps import AppConfig


class NotificationsConfig(AppConfig):
    name = "apps.notifications"
    label = "notifications"
    default = True

    def ready(self) -> None:
        from apps.notifications import (
            eligibility,  # noqa: F401 - registers the security rules
            housekeeping,
            notices,
        )

        notices.register()
        housekeeping.register_task(
            housekeeping.IN_APP_TASK, housekeeping.purge_in_app_notifications
        )

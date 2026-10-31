from django.apps import AppConfig


class NotificationsConfig(AppConfig):
    name = "apps.notifications"
    label = "notifications"
    default = True

    def ready(self) -> None:
        from apps.notifications import eligibility  # noqa: F401 - registers the security rules

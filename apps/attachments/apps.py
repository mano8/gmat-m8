from django.apps import AppConfig


class AttachmentsConfig(AppConfig):
    name = "apps.attachments"
    label = "attachments"
    default = True

    def ready(self) -> None:
        from apps.attachments import checks, housekeeping  # noqa: F401 - registers the check
        from apps.notifications.housekeeping import register_task

        # Expired leases and the media purge run with the worker's hourly housekeeping.
        register_task("attachment_reservations", housekeeping.release_expired_reservations)
        register_task("attachment_media", housekeeping.purge_media)

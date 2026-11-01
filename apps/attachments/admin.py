"""Read-only admin for attachments (plan §5, "Django Admin").

Files are added and removed only from the documents' pages, through the services; the admin
shows the rows and never the files (downloads go through ``views.download``).
"""

from django.contrib import admin

from apps.attachments.models import (
    Attachment,
    AttachmentReservation,
    MediaDeletion,
    StorageQuota,
)
from apps.core.admin import ReadOnlyModelAdmin


@admin.register(Attachment)
class AttachmentAdmin(ReadOnlyModelAdmin):
    list_display = ("file_name", "content_type", "size_bytes", "uploaded_at", "removed_at")
    list_filter = ("content_type",)
    list_select_related = ("request", "receipt", "handover")
    search_fields = ("file_name", "request__number", "receipt__number", "handover__number")
    exclude = ("storage_key",)


@admin.register(AttachmentReservation)
class AttachmentReservationAdmin(ReadOnlyModelAdmin):
    list_display = ("state", "reserved_bytes", "lease_until", "created_at", "finished_at")
    list_filter = ("state",)


@admin.register(StorageQuota)
class StorageQuotaAdmin(ReadOnlyModelAdmin):
    list_display = ("limit_bytes", "used_bytes", "reserved_bytes", "alert_raised_at")


@admin.register(MediaDeletion)
class MediaDeletionAdmin(ReadOnlyModelAdmin):
    list_display = ("size_bytes", "delete_after")
    exclude = ("storage_key",)

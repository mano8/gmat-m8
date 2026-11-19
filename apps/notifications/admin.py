"""Admin for the business notices' settings: read-only (ADR-0030, M15.1).

Group administrators change them on the application's screens (M15.3). The outbox itself
is not in the admin: its bodies and recipients are read by nobody but the worker.
"""

from django.contrib import admin

from apps.core.admin import ReadOnlyModelAdmin
from apps.notifications.models import NoticeDefault, NoticeSetting


@admin.register(NoticeDefault)
class NoticeDefaultAdmin(ReadOnlyModelAdmin):
    list_display = ("notice", "channel", "enabled", "changed_by", "changed_at")
    list_filter = ("notice", "channel", "enabled")
    list_select_related = ("changed_by",)
    fields = ("notice", "channel", "enabled", "changed_by", "changed_at")


@admin.register(NoticeSetting)
class NoticeSettingAdmin(ReadOnlyModelAdmin):
    list_display = ("user", "notice", "channel", "mode", "set_by", "set_at")
    list_filter = ("notice", "channel", "mode")
    list_select_related = ("user", "set_by")
    fields = ("user", "notice", "channel", "mode", "set_by", "set_at")

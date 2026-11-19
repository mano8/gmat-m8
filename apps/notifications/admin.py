"""Admin for the business notices' settings: read-only (ADR-0030, M15.1, M17.1).

Group administrators change theirs on the application's screens (M15.3), each person their own
switches and choices on *My notifications* (M17.4). The outbox itself
is not in the admin: its bodies and recipients are read by nobody but the worker.
"""

from django.contrib import admin

from apps.core.admin import ReadOnlyModelAdmin
from apps.notifications.models import ChannelSwitch, NoticeChoice, NoticeDefault, NoticeSetting


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


@admin.register(NoticeChoice)
class NoticeChoiceAdmin(ReadOnlyModelAdmin):
    list_display = ("user", "notice", "channel", "enabled", "changed_at")
    list_filter = ("notice", "channel", "enabled")
    list_select_related = ("user",)
    fields = ("user", "notice", "channel", "enabled", "changed_at")


@admin.register(ChannelSwitch)
class ChannelSwitchAdmin(ReadOnlyModelAdmin):
    list_display = ("user", "channel", "enabled", "changed_at")
    list_filter = ("channel", "enabled")
    list_select_related = ("user",)
    fields = ("user", "channel", "enabled", "changed_at")

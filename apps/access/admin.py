"""Admin of the access engine: the access state, read-only like every model (ADR-0030)."""

from django.contrib import admin

from apps.access.models import AccessState
from apps.core.admin import ReadOnlyModelAdmin


@admin.register(AccessState)
class AccessStateAdmin(ReadOnlyModelAdmin):
    list_display = ("id", "org_generation")

"""Read-only admin for the access state (plan §5, "Django Admin")."""

from django.contrib import admin

from apps.access.models import AccessState
from apps.core.admin import ReadOnlyModelAdmin


@admin.register(AccessState)
class AccessStateAdmin(ReadOnlyModelAdmin):
    list_display = ("id", "org_generation")

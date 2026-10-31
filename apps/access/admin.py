"""Admin pieces of the access engine (plan §5, "Django Admin").

- ``CapabilityModelAdmin``: the admin's add and change permissions come from the access
  engine, for the object when the model is scoped. Viewing needs only what the admin site
  admits (platform operators and group administrators, with session MFA).
- The access state itself is read-only.
"""

from typing import ClassVar

from django.contrib import admin
from django.db.models import Model
from django.http import HttpRequest

from apps.access.middleware import access_for
from apps.access.models import AccessState
from apps.access.scopes import ScopedQuerySet
from apps.core.admin import ReadOnlyModelAdmin, ServiceBackedModelAdmin


class CapabilityModelAdmin(ServiceBackedModelAdmin):
    """Add and change need ``edit_capability`` (within scope for a scoped model)."""

    edit_capability: ClassVar[str]

    def may_edit(self, request: HttpRequest, obj: Model | None = None) -> bool:
        ctx = access_for(request)
        if obj is not None and isinstance(type(obj)._default_manager.all(), ScopedQuerySet):
            return ctx.can(self.edit_capability, obj)
        return ctx.has(self.edit_capability)


@admin.register(AccessState)
class AccessStateAdmin(ReadOnlyModelAdmin):
    list_display = ("id", "org_generation")

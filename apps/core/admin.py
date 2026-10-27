"""Admin base classes (plan §5, "Django Admin").

Every ``ModelAdmin`` is built on ``ServiceBackedModelAdmin``; a system check refuses any
other (``apps.core.checks``). Saving goes through the same services as the app, so
generations are bumped, audit is written and preconditions apply. Nothing is ever deleted
from the admin: records are deactivated through their service instead.
"""

from typing import Any

from django.contrib import admin
from django.contrib.admin import ActionLocation
from django.db.models import Model
from django.http import HttpRequest


class ServiceBackedModelAdmin(admin.ModelAdmin):  # type: ignore[type-arg]
    """Saves through ``save_via_service``; deletes and the bulk delete action are disabled.

    Subclasses for editable master data implement ``save_via_service``. Extra bulk actions
    are allowed only when they call a service.
    """

    def save_via_service(self, request: HttpRequest, obj: Any, form: Any, change: bool) -> None:
        raise NotImplementedError(f"{type(self).__name__} must save through a service")

    def save_model(self, request: HttpRequest, obj: Any, form: Any, change: bool) -> None:
        self.save_via_service(request, obj, form, change)

    def delete_model(self, request: HttpRequest, obj: Any) -> None:
        raise PermissionError("Records are deactivated through their service, never deleted")

    def delete_queryset(self, request: HttpRequest, queryset: Any) -> None:
        raise PermissionError("Records are deactivated through their service, never deleted")

    def has_delete_permission(self, request: HttpRequest, obj: Model | None = None) -> bool:
        return False

    def get_actions(
        self, request: HttpRequest, action_location: ActionLocation = ActionLocation.CHANGE_LIST
    ) -> dict[str, Any]:
        actions = super().get_actions(request, action_location=action_location)
        actions.pop("delete_selected", None)
        return actions


class ReadOnlyModelAdmin(ServiceBackedModelAdmin):
    """View permission only: users, access records, documents, outbox and audit."""

    def has_add_permission(self, request: HttpRequest) -> bool:
        return False

    def has_change_permission(self, request: HttpRequest, obj: Model | None = None) -> bool:
        return False

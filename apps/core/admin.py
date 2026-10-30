"""Admin base classes (plan §5, "Django Admin").

Every ``ModelAdmin`` is built on ``ServiceBackedModelAdmin``; a system check refuses any
other (``apps.core.checks``). Saving goes through the same services as the app, so
generations are bumped, audit is written and preconditions apply. Nothing is ever deleted
from the admin: records are deactivated through their service instead.
"""

from typing import Any

from django.contrib import admin, messages
from django.contrib.admin import ActionLocation
from django.core.exceptions import ValidationError
from django.db.models import Model
from django.http import HttpRequest, HttpResponse, HttpResponseRedirect


class ServiceBackedModelAdmin(admin.ModelAdmin):  # type: ignore[type-arg]
    """Saves through ``save_via_service``; deletes and the bulk delete action are disabled.

    Whoever the admin site admits may view; nobody may add or change unless a subclass's
    ``may_edit`` says so. Subclasses for editable master data implement ``may_edit`` and
    ``save_via_service``. Extra bulk actions are allowed only when they call a service.
    """

    def may_edit(self, request: HttpRequest) -> bool:
        """Whether this user may add and change records here; the service checks again."""
        return False

    def has_module_permission(self, request: HttpRequest) -> bool:
        return self.admin_site.has_permission(request)

    def has_view_permission(self, request: HttpRequest, obj: Model | None = None) -> bool:
        return self.admin_site.has_permission(request)

    def has_add_permission(self, request: HttpRequest) -> bool:
        return self.admin_site.has_permission(request) and self.may_edit(request)

    def has_change_permission(self, request: HttpRequest, obj: Model | None = None) -> bool:
        return self.admin_site.has_permission(request) and self.may_edit(request)

    def save_via_service(self, request: HttpRequest, obj: Any, form: Any, change: bool) -> None:
        raise NotImplementedError(f"{type(self).__name__} must save through a service")

    def form_values(self, form: Any, names: Any = None) -> dict[str, Any]:
        """Cleaned form values keyed for a service call (``branch`` → ``branch_id``).

        ``names`` defaults to the fields the user changed.
        """
        values: dict[str, Any] = {}
        for name in form.changed_data if names is None else names:
            field = self.model._meta.get_field(name)
            value = form.cleaned_data[name]
            if field.is_relation:
                values[str(field.attname)] = value.pk if value is not None else None
            else:
                values[name] = value
        return values

    @staticmethod
    def adopt(obj: Model, saved: Model) -> None:
        """Point the form's instance at the row the service saved (for logging, redirect)."""
        obj.pk = saved.pk
        obj._state.adding = False
        obj._state.db = saved._state.db

    def changeform_view(
        self,
        request: HttpRequest,
        object_id: str | None = None,
        form_url: str = "",
        extra_context: dict[str, Any] | None = None,
    ) -> HttpResponse:
        """A service's refusal (checked under its locks) is shown, not a server error.

        The form validates the same rules first; this covers what only the service can
        see, such as open documents or a concurrent change. Nothing was saved.
        """
        try:
            return super().changeform_view(request, object_id, form_url, extra_context)
        except ValidationError as exc:
            for message in exc.messages:
                self.message_user(request, message, level=messages.ERROR)
            return HttpResponseRedirect(request.get_full_path())

    def save_model(self, request: HttpRequest, obj: Any, form: Any, change: bool) -> None:
        self.save_via_service(request, obj, form, change)

    def save_related(self, request: HttpRequest, form: Any, formsets: Any, change: bool) -> None:
        """Many-to-many fields and inlines are the service's job, never a direct save.

        Django would call ``form.save_m2m()`` and save each inline formset here, outside
        the service. ``save_via_service`` reads them from the form instead; an admin that
        shows inlines must override this and hand them to a service too.
        """
        if formsets:
            raise NotImplementedError(f"{type(self).__name__} must save its inlines via a service")

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

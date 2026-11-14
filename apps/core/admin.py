"""Admin base class (plan §5, "Django Admin"; ADR-0030).

The admin is optional (``DJANGO_ADMIN_ENABLED``) and, while it is on, read-only for every
model: what a person administers has a screen in the app, which calls the services. Every
``ModelAdmin`` is a ``ReadOnlyModelAdmin`` and leaves its write paths alone; the system
checks ``gmat.E005`` and ``gmat.E024`` (``apps.core.checks``) and the ``editable-admin``
rule of ``scripts/check_access_rules.py`` refuse anything else, so a model cannot become
editable in the admin again by a subclass.
"""

from typing import Any, NoReturn

from django.contrib import admin
from django.contrib.admin import ActionLocation, SimpleListFilter
from django.core.exceptions import PermissionDenied
from django.db.models import Model
from django.db.models.constants import LOOKUP_SEP
from django.http import HttpRequest

# The methods through which an admin adds, changes or deletes a record, or offers a view or
# an action that could. A subclass of ``ReadOnlyModelAdmin`` never redefines one: compared by
# identity in ``gmat.E024`` and by name in ``scripts/check_access_rules.py``, which keeps its
# own copy of this tuple (a test compares them).
READ_ONLY_METHODS = (
    "has_add_permission",
    "has_change_permission",
    "has_delete_permission",
    "save_model",
    "save_related",
    "save_formset",
    "delete_model",
    "delete_queryset",
    "get_actions",
    "get_urls",
    "changeform_view",
)


def _refuse(admin_class: type) -> NoReturn:
    raise PermissionDenied(f"{admin_class.__name__} is read-only: changes go through the app")


class ReadOnlyModelAdmin(admin.ModelAdmin):  # type: ignore[type-arg]
    """View permission only, for whoever the admin site admits; nothing is written.

    Django checks the add, change and delete permissions before it saves or deletes, so the
    pages answer 403 to a write; the save and delete methods refuse too, in case a path
    reaches them anyway. No bulk action is offered, not even Django's delete.
    """

    def lookup_allowed(self, lookup: str, value: str, request: HttpRequest) -> bool:
        """Only what the page offers filters it: its list filters and its date hierarchy.

        Django accepts a query-string lookup on any local field, so the result count would
        read back a column the page never shows (``?password__startswith=…`` on a user, a
        price, an audit payload), one guess at a time. Anything else is a 400.
        """
        offered = self._filter_fields(request)
        if lookup not in offered and lookup.split(LOOKUP_SEP, 1)[0] not in offered:
            return False
        return super().lookup_allowed(lookup, value, request)

    def _filter_fields(self, request: HttpRequest) -> set[str]:
        """The field of every list filter (or its parameter) and of the date hierarchy."""
        fields = {self.date_hierarchy} if self.date_hierarchy else set()
        for item in self.get_list_filter(request):
            if isinstance(item, type) and issubclass(item, SimpleListFilter):
                fields.add(str(item.parameter_name))
            else:
                path = item[0] if isinstance(item, list | tuple) else item
                fields.add(str(path).split(LOOKUP_SEP, 1)[0])
        return fields

    def has_module_permission(self, request: HttpRequest) -> bool:
        return self.admin_site.has_permission(request)

    def has_view_permission(self, request: HttpRequest, obj: Model | None = None) -> bool:
        return self.admin_site.has_permission(request)

    def has_add_permission(self, request: HttpRequest) -> bool:
        return False

    def has_change_permission(self, request: HttpRequest, obj: Model | None = None) -> bool:
        return False

    def has_delete_permission(self, request: HttpRequest, obj: Model | None = None) -> bool:
        return False

    def save_model(self, request: HttpRequest, obj: Any, form: Any, change: bool) -> None:
        _refuse(type(self))

    def save_related(self, request: HttpRequest, form: Any, formsets: Any, change: bool) -> None:
        _refuse(type(self))

    def save_formset(self, request: HttpRequest, form: Any, formset: Any, change: bool) -> None:
        _refuse(type(self))

    def delete_model(self, request: HttpRequest, obj: Any) -> None:
        _refuse(type(self))

    def delete_queryset(self, request: HttpRequest, queryset: Any) -> None:
        _refuse(type(self))

    def get_actions(
        self, request: HttpRequest, action_location: ActionLocation = ActionLocation.CHANGE_LIST
    ) -> dict[str, Any]:
        return {}

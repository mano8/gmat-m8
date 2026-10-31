"""Read-only admin for projects and assignments (plan §5, "Django Admin").

Projects and their assignments are managed in the app (M2.6), where material managers
work within their scope.
"""

from django.contrib import admin

from apps.core.admin import ReadOnlyModelAdmin
from apps.projects.models import Project, ProjectAssignment


@admin.register(Project)
class ProjectAdmin(ReadOnlyModelAdmin):
    list_display = ("code", "name", "branch", "procurement_branch", "status")
    list_filter = ("status",)
    list_select_related = ("branch__company", "procurement_branch__company")
    search_fields = ("code", "name", "client_name")


@admin.register(ProjectAssignment)
class ProjectAssignmentAdmin(ReadOnlyModelAdmin):
    list_display = ("project", "user", "kind", "valid_from", "valid_until", "revoked_at")
    list_filter = ("kind",)
    list_select_related = ("project", "user")
    search_fields = ("project__code", "user__email")

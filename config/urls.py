from django.conf import settings
from django.contrib import admin
from django.urls import URLPattern, URLResolver, include, path

# The admin exists only while it is on (ADR-0030): off, its prefix is an unknown path (404).
admin_urls: list[URLPattern | URLResolver] = (
    [path(f"{settings.ADMIN_URL_PATH}/", admin.site.urls)] if settings.ADMIN_ENABLED else []
)

urlpatterns = [
    *admin_urls,
    path("accounts/", include("apps.accounts.urls")),
    path("invite/", include("apps.accounts.invitation_urls")),
    path("account/notifications/", include("apps.notifications.urls")),
    path("account/email-change/", include("apps.accounts.email_change_urls")),
    path("account/sessions/", include("apps.accounts.session_urls")),
    path("projects/", include("apps.projects.urls")),
    path("catalog/", include("apps.catalog.urls")),
    path("requests/", include("apps.material_requests.urls")),
    path("suppliers/", include("apps.suppliers.urls")),
    path("purchasing/", include("apps.purchasing.urls")),
    path("receiving/", include("apps.receiving.urls")),
    path("handovers/", include("apps.handovers.urls")),
    path("attachments/", include("apps.attachments.urls")),
    path("org/", include("apps.organizations.urls")),
    path("org/", include("apps.locations.urls")),
    path("audit/", include("apps.audit.urls")),
    path("exports/", include("apps.exports.urls")),
    path("dashboard/", include("apps.dashboards.urls")),
    path("i18n/", include("django.conf.urls.i18n")),
    path("", include("apps.core.urls")),
]

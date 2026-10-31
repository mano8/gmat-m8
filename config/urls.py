from django.conf import settings
from django.contrib import admin
from django.urls import include, path

urlpatterns = [
    path(f"{settings.ADMIN_URL_PATH}/", admin.site.urls),
    path("accounts/", include("apps.accounts.urls")),
    path("invite/", include("apps.accounts.invitation_urls")),
    path("account/email-change/", include("apps.accounts.email_change_urls")),
    path("projects/", include("apps.projects.urls")),
    path("org/", include("apps.organizations.urls")),
    path("i18n/", include("django.conf.urls.i18n")),
    path("", include("apps.core.urls")),
]

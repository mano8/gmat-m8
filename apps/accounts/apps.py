from django.apps import AppConfig
from django.contrib.admin.apps import AdminConfig as DjangoAdminConfig


class AccountsConfig(AppConfig):
    default = True
    name = "apps.accounts"
    label = "accounts"

    def ready(self) -> None:
        from allauth.mfa.signals import authenticator_removed
        from django.contrib.auth.signals import user_logged_in

        from apps.accounts import signals

        user_logged_in.connect(signals.on_user_logged_in, dispatch_uid="gmat-session-start")
        authenticator_removed.connect(
            signals.on_authenticator_removed, dispatch_uid="gmat-authenticator-removed"
        )


class AdminConfig(DjangoAdminConfig):
    """Installs ``SecureAdminSite`` as the default admin site (replaces django.contrib.admin)."""

    default = False
    default_site = "apps.accounts.admin_site.SecureAdminSite"

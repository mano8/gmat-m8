from django.apps import AppConfig
from django.contrib.admin.apps import AdminConfig as DjangoAdminConfig


class AccountsConfig(AppConfig):
    default = True
    name = "apps.accounts"
    label = "accounts"

    def ready(self) -> None:
        from allauth.mfa.signals import authenticator_removed
        from django.contrib.auth.signals import user_logged_in

        from apps.accounts import eligibility, housekeeping, signals
        from apps.notifications.housekeeping import register_task

        user_logged_in.connect(signals.on_user_logged_in, dispatch_uid="gmat-session-start")
        authenticator_removed.connect(
            signals.on_authenticator_removed, dispatch_uid="gmat-authenticator-removed"
        )
        eligibility.register()
        register_task(housekeeping.TASK_NAME, housekeeping.apply_due_email_changes)


class AdminConfig(DjangoAdminConfig):
    """Installs ``SecureAdminSite`` as the default admin site (replaces django.contrib.admin)."""

    default = False
    default_site = "apps.accounts.admin_site.SecureAdminSite"

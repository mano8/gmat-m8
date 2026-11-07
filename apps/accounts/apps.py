from django.apps import AppConfig
from django.contrib.admin.apps import AdminConfig as DjangoAdminConfig


class AccountsConfig(AppConfig):
    default = True
    name = "apps.accounts"
    label = "accounts"

    def ready(self) -> None:
        from allauth.account import signals as account
        from allauth.mfa.signals import (
            authenticator_added,
            authenticator_removed,
            authenticator_reset,
        )
        from django.contrib.auth.signals import user_logged_in, user_logged_out

        from apps.accounts import eligibility, housekeeping, security_log, signals
        from apps.notifications.housekeeping import register_task

        user_logged_in.connect(signals.on_user_logged_in, dispatch_uid="gmat-session-start")
        user_logged_out.connect(signals.on_user_logged_out, dispatch_uid="gmat-session-end")
        authenticator_removed.connect(
            signals.on_authenticator_removed, dispatch_uid="gmat-authenticator-removed"
        )
        for signal in (authenticator_added, authenticator_reset):
            signal.connect(
                signals.on_recovery_codes_changed, dispatch_uid="gmat-recovery-codes-changed"
            )
        account.password_changed.connect(
            signals.on_password_changed, dispatch_uid="gmat-audit-password-changed"
        )
        account.password_set.connect(
            signals.on_password_set, dispatch_uid="gmat-audit-password-set"
        )
        account.password_reset.connect(
            signals.on_password_reset, dispatch_uid="gmat-audit-password-reset"
        )
        security_log.connect()
        eligibility.register()
        register_task(housekeeping.TASK_NAME, housekeeping.apply_due_email_changes)
        register_task(housekeeping.SESSIONS_TASK_NAME, housekeeping.purge_ended_sessions)


class AdminConfig(DjangoAdminConfig):
    """Installs ``SecureAdminSite`` as the default admin site (replaces django.contrib.admin)."""

    default = False
    default_site = "apps.accounts.admin_site.SecureAdminSite"

"""Authentication URLs: allauth's, minus everything this project keeps closed.

There is deliberately no ``app_name``: allauth reverses its own URL names globally.

- Sign-up is closed (accounts arrive through invitations, M2.6), so its URL does not exist.
- allauth's email-management and email-confirmation views are disabled (F59): addresses
  change only through the administrator flow, so a stolen session cannot redirect password
  resets to an attacker's mailbox.
- Login by code is off, so every login takes the password and then MFA.
- Setting a first password happens in invitation acceptance, not here.
- The password-reset key view is replaced by the locked redemption (``views``).
"""

from allauth.account import urls as account_urls
from allauth.mfa import urls as mfa_urls
from django.urls import URLPattern, URLResolver, include, path, re_path

from apps.accounts import views

DISABLED_URL_NAMES = frozenset(
    {
        "account_signup",
        "account_signup_by_passkey",
        "account_email",
        "account_email_verification_sent",
        "account_confirm_email",
        "account_request_login_code",
        "account_confirm_login_code",
        "account_set_password",
        "account_change_phone",
        "account_verify_phone",
    }
)
REPLACED_URL_NAMES = frozenset({"account_reset_password_from_key"})

urlpatterns: list[URLPattern | URLResolver] = [
    re_path(
        r"^password/reset/key/(?P<uidb36>[0-9A-Za-z]+)-(?P<key>.+)/$",
        views.password_reset_from_key,
        name="account_reset_password_from_key",
    ),
    *(
        pattern
        for pattern in account_urls.urlpatterns
        if getattr(pattern, "name", None) not in DISABLED_URL_NAMES | REPLACED_URL_NAMES
    ),
    path("2fa/", include(mfa_urls)),
]

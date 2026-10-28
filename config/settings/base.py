"""Settings shared by every environment. Environment-specific modules extend this one."""

import re
from pathlib import Path

from django.core.exceptions import ImproperlyConfigured
from django.utils.csp import CSP

from config.env import env_int, env_str

BASE_DIR = Path(__file__).resolve().parent.parent.parent

INSTALLED_APPS = [
    "apps.accounts.apps.AdminConfig",  # django.contrib.admin with SecureAdminSite
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "apps.core.apps.StaticFilesConfig",  # django.contrib.staticfiles, minus the Tailwind input
    "django.forms",  # lets the project override the form templates (TemplatesSetting)
    "allauth",
    "allauth.account",
    "allauth.mfa",
    # Project apps last: their admin modules replace third-party registrations.
    "apps.core",
    "apps.accounts.apps.AccountsConfig",
]

# Order matters (plan §3.6): session auth → session epoch → MFA enforcement.
MIDDLEWARE = [
    "apps.core.middleware.RequestIdMiddleware",
    "django.middleware.security.SecurityMiddleware",
    "django.middleware.csp.ContentSecurityPolicyMiddleware",
    "whitenoise.middleware.WhiteNoiseMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.locale.LocaleMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django_htmx.middleware.HtmxMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "apps.accounts.middleware.SessionGuardMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
    "allauth.account.middleware.AccountMiddleware",
    "apps.accounts.middleware.MFARequiredMiddleware",
    "apps.core.middleware.SecurityHeadersMiddleware",
    "apps.core.middleware.CacheUnavailableMiddleware",
    # Innermost: gives a bare 405/409/429 answer a page (the security headers apply after).
    "apps.core.middleware.ErrorPagesMiddleware",
]

ROOT_URLCONF = "config.urls"

# URL prefix of the Django admin, without slashes. The compose stack passes the same value to
# Caddy, which lets only the allowed networks reach it (plan §7.3).
ADMIN_URL_PATH = env_str("DJANGO_ADMIN_URL_PATH", "admin")
if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", ADMIN_URL_PATH):
    raise ImproperlyConfigured(
        "DJANGO_ADMIN_URL_PATH must be 1 to 64 letters, digits, '-' or '_' (no slashes)"
    )
WSGI_APPLICATION = "config.wsgi.application"
ASGI_APPLICATION = "config.asgi.application"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [BASE_DIR / "templates"],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.template.context_processors.i18n",
                "apps.core.context_processors.ui",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ],
        },
    },
]

# Forms render through the project's templates (templates/django/forms), styled by the UI shell.
FORM_RENDERER = "django.forms.renderers.TemplatesSetting"

AUTH_USER_MODEL = "accounts.User"
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

# allauth's backend only: every login goes through allauth (rate limits, MFA stage).
AUTHENTICATION_BACKENDS = ["allauth.account.auth_backends.AuthenticationBackend"]
LOGIN_URL = "account_login"
LOGIN_REDIRECT_URL = "core:home"

PASSWORD_HASHERS = [
    "django.contrib.auth.hashers.Argon2PasswordHasher",
]
AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {
        "NAME": "django.contrib.auth.password_validation.MinimumLengthValidator",
        "OPTIONS": {"min_length": 12},
    },
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]
PASSWORD_RESET_TIMEOUT = 3600  # one hour (plan §5)

# --- allauth (plan §5, "Identity, sessions and MFA"; floor 65.20.1) ---------------------
ACCOUNT_ADAPTER = "apps.accounts.adapters.AccountAdapter"
ACCOUNT_FORMS = {"reset_password_from_key": "apps.accounts.forms.ResetPasswordKeyForm"}
ACCOUNT_LOGIN_METHODS = {"email"}
# Sign-up is closed; the login form still reads the password field from this list.
ACCOUNT_SIGNUP_FIELDS = ["email*", "password1*"]
ACCOUNT_USER_MODEL_USERNAME_FIELD = None
ACCOUNT_UNIQUE_EMAIL = True
ACCOUNT_MAX_EMAIL_ADDRESSES = 1
ACCOUNT_CHANGE_EMAIL = False
# Emails are verified by invitation acceptance and administrator email changes (M2.6).
ACCOUNT_EMAIL_VERIFICATION = "none"
ACCOUNT_PREVENT_ENUMERATION = True
ACCOUNT_EMAIL_UNKNOWN_ACCOUNTS = False
ACCOUNT_LOGOUT_ON_GET = False
ACCOUNT_LOGOUT_ON_PASSWORD_CHANGE = True
ACCOUNT_LOGIN_ON_PASSWORD_RESET = False
ACCOUNT_LOGIN_BY_CODE_ENABLED = False
ACCOUNT_PASSWORD_RESET_BY_CODE_ENABLED = False
ACCOUNT_SESSION_REMEMBER = True
ACCOUNT_DEFAULT_HTTP_PROTOCOL = "https"
ACCOUNT_EMAIL_SUBJECT_PREFIX = "[gmat-m8] "
ACCOUNT_RATE_LIMITS = {
    "change_password": "5/m/user",
    "reset_password": "20/m/ip,5/m/key",
    "reauthenticate": "10/m/user",
    "reset_password_from_key": "20/m/ip",
    "login": "30/m/ip",
    "login_failed": "10/m/ip,5/5m/key",
}
# Caddy is the only proxy and replaces any client-sent X-Forwarded-For (plan §5).
ALLAUTH_TRUSTED_PROXY_COUNT = 1

MFA_ADAPTER = "apps.accounts.adapters.MFAAdapter"
MFA_SUPPORTED_TYPES = ["totp", "recovery_codes", "webauthn"]
MFA_PASSKEY_LOGIN_ENABLED = False
MFA_PASSKEY_SIGNUP_ENABLED = False
MFA_TRUST_ENABLED = False
MFA_TOTP_TOLERANCE = 0
MFA_TOTP_ISSUER = "gmat-m8"

# --- security cache (rate limits; plan §5 "Rate-limit storage") -----------------------
CACHES = {
    "default": {
        "BACKEND": "apps.core.cache.SecurityDatabaseCache",
        "LOCATION": "django_cache",
        "OPTIONS": {"MAX_ENTRIES": env_int("SECURITY_CACHE_MAX_ENTRIES", 50_000, minimum=100)},
    }
}

# --- sessions and cookies -------------------------------------------------------------
SESSION_COOKIE_AGE = env_int("DJANGO_SESSION_AGE_SECONDS", 14 * 24 * 3600, minimum=300)
PRIVILEGED_SESSION_AGE = env_int("DJANGO_PRIVILEGED_SESSION_AGE_SECONDS", 12 * 3600, minimum=300)
SESSION_COOKIE_NAME = "__Host-sessionid"
CSRF_COOKIE_NAME = "__Host-csrftoken"
SESSION_COOKIE_SECURE = True
CSRF_COOKIE_SECURE = True
SESSION_COOKIE_HTTPONLY = True
CSRF_COOKIE_HTTPONLY = True  # htmx sends the token from the page (hx-headers), not the cookie
SESSION_COOKIE_SAMESITE = "Lax"
CSRF_COOKIE_SAMESITE = "Lax"

# --- headers (plan §5, Web) -----------------------------------------------------------
X_FRAME_OPTIONS = "DENY"
SECURE_CONTENT_TYPE_NOSNIFF = True
SECURE_REFERRER_POLICY = "same-origin"
SECURE_CROSS_ORIGIN_OPENER_POLICY = "same-origin"
SECURE_CSP = {
    "default-src": [CSP.SELF],
    "script-src": [CSP.SELF],
    "style-src": [CSP.SELF],
    "img-src": [CSP.SELF, "data:", "blob:"],
    "font-src": [CSP.SELF],
    "connect-src": [CSP.SELF],
    "form-action": [CSP.SELF],
    "frame-ancestors": [CSP.NONE],
    "base-uri": [CSP.SELF],
    "object-src": [CSP.NONE],
}

# --- email ----------------------------------------------------------------------------
# Never SMTP inside a request. M2.5 replaces this with the outbox backend.
MAILERS = {"default": {"BACKEND": "django.core.mail.backends.console.EmailBackend"}}
DEFAULT_FROM_EMAIL = env_str("DJANGO_DEFAULT_FROM_EMAIL", "gmat-m8 <no-reply@localhost>")

LANGUAGE_CODE = "es"
LANGUAGES = [("es", "Español"), ("en", "English"), ("fr", "Français")]
LOCALE_PATHS = [BASE_DIR / "locale"]
# The language choice is a cookie like the session: __Host- prefixed, Secure, not readable by
# scripts (the switcher is a plain POST form).
LANGUAGE_COOKIE_NAME = "__Host-language"
LANGUAGE_COOKIE_SECURE = True
LANGUAGE_COOKIE_HTTPONLY = True
LANGUAGE_COOKIE_SAMESITE = "Lax"
LANGUAGE_COOKIE_AGE = 365 * 24 * 3600
TIME_ZONE = "UTC"
USE_I18N = True
USE_TZ = True

STATIC_URL = "/static/"
STATIC_ROOT = BASE_DIR / "staticfiles"
# Hashed file names and compressed copies, served by WhiteNoise with a far-future cache header.
# `collectstatic` (image build) and production use this; dev and tests keep the plain storage.
STORAGES = {
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    "staticfiles": {"BACKEND": "whitenoise.storage.CompressedManifestStaticFilesStorage"},
}
STATICFILES_DIRS = [BASE_DIR / "static"] if (BASE_DIR / "static").is_dir() else []

# --- logging (plan §5, "Logging and secret redaction") --------------------------------
# One JSON handler with the redaction filter. The "django" and "django.server" loggers are
# redefined so Django's default handlers (including AdminEmailHandler) never receive a
# record; a system check refuses any handler without the redaction filter.
LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "filters": {
        "request_id": {"()": "config.logging.RequestIdFilter"},
        "redact": {"()": "config.logging.RedactionFilter"},
    },
    "formatters": {"json": {"()": "config.logging.JsonFormatter"}},
    "handlers": {
        "console": {
            "class": "logging.StreamHandler",
            "formatter": "json",
            "filters": ["request_id", "redact"],
        },
    },
    "root": {"handlers": ["console"], "level": env_str("DJANGO_LOG_LEVEL", "INFO")},
    "loggers": {
        "django": {"handlers": ["console"], "level": "INFO", "propagate": False},
        "django.server": {"handlers": ["console"], "level": "INFO", "propagate": False},
    },
}
DEFAULT_EXCEPTION_REPORTER_FILTER = "config.logging.RedactingExceptionReporterFilter"


def database_from_env() -> dict[str, object]:
    """PostgreSQL connection from ``DB_*``; a failed connection never waits forever."""
    return {
        "ENGINE": "django.db.backends.postgresql",
        "NAME": env_str("DB_NAME"),
        "USER": env_str("DB_USER"),
        "PASSWORD": env_str("DB_PASSWORD"),
        "HOST": env_str("DB_HOST"),
        "PORT": env_int("DB_PORT", 5432, minimum=1, maximum=65535),
        "CONN_MAX_AGE": 0,
        "OPTIONS": {"connect_timeout": 5},
    }

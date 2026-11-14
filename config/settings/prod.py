"""Production settings. Every value that matters is required and validated: fail closed."""

from django.core.exceptions import ImproperlyConfigured

from config.env import (
    env_bool,
    env_fernet_keys,
    env_int,
    env_list,
    env_secret,
    env_str,
    non_production_fernet_keys,
)
from config.settings.base import *  # noqa: F403
from config.settings.base import OUTBOX_BACKEND, database_from_env, outbound_mailer_from_env

DEBUG = False
if env_bool("DJANGO_DEBUG", False):
    raise ImproperlyConfigured("DJANGO_DEBUG must not be enabled in production")

SECRET_KEY = env_secret("DJANGO_SECRET_KEY", min_length=50)

# The admin is off unless the maintainer turns it on (ADR-0030); deploy.sh then requires
# ADMIN_ALLOWED_CIDRS, the networks Caddy lets reach it.
ADMIN_ENABLED = env_bool("DJANGO_ADMIN_ENABLED", False)

ALLOWED_HOSTS = env_list("DJANGO_ALLOWED_HOSTS")
DATABASES = {"default": database_from_env()}
if "*" in ALLOWED_HOSTS:
    raise ImproperlyConfigured("DJANGO_ALLOWED_HOSTS must not contain a wildcard")

CSRF_TRUSTED_ORIGINS = env_list("DJANGO_CSRF_TRUSTED_ORIGINS")
if not all(origin.startswith("https://") for origin in CSRF_TRUSTED_ORIGINS):
    raise ImproperlyConfigured("DJANGO_CSRF_TRUSTED_ORIGINS must all be https:// origins")
# Links in email and the bootstrap link point at the first trusted origin.
PUBLIC_BASE_URL = CSRF_TRUSTED_ORIGINS[0]

# Separate from SECRET_KEY; newest first. The development and test keys are refused.
MFA_ENCRYPTION_KEYS = env_fernet_keys("MFA_ENCRYPTION_KEYS", refused=non_production_fernet_keys())
# Seals sensitive outbox bodies (plan §5). Its own keys: never an MFA key.
OUTBOX_ENCRYPTION_KEYS = env_fernet_keys(
    "OUTBOX_ENCRYPTION_KEYS",
    refused=non_production_fernet_keys() | frozenset(MFA_ENCRYPTION_KEYS),
)

# The stack's fixed paths: the volumes compose mounts in `web` (M8.3, ADR-0015). gmat.E022 refuses
# an empty value here, which would store unscanned files.
ATTACHMENT_SCAN_SOCKET = env_str(
    "ATTACHMENT_SCAN_SOCKET", "/run/gmat/clamd/clamd.sock", min_length=0
)
ATTACHMENT_DECODER_SOCKET = env_str(
    "ATTACHMENT_DECODER_SOCKET", "/run/gmat/decoder/decoder.sock", min_length=0
)

# TLS terminates at Caddy, the only proxy in front of the app. The internal health checks
# reach the app over plain HTTP, so they are exempt from the redirect.
SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
SECURE_SSL_REDIRECT = True
SECURE_REDIRECT_EXEMPT = [r"^healthz/$", r"^readyz/$"]
SESSION_COOKIE_SECURE = True
CSRF_COOKIE_SECURE = True
# HSTS is the maintainer's to turn on, one switch at a time, once the domain has proved it
# serves HTTPS (M8.3, exception X3): a browser that has seen the header refuses plain HTTP
# for as long as it says, and nothing here can take that back. Nothing turns on by itself. A
# deploy check does not fail on a switch that is off (Django's warning about that switch is
# silenced, and only while it is off); `web` logs a warning at startup instead
# (`apps.accounts.security_log.warn_hsts_off`).
SECURE_HSTS_SECONDS = env_int("DJANGO_HSTS_SECONDS", 0, minimum=0)
SECURE_HSTS_INCLUDE_SUBDOMAINS = env_bool("DJANGO_HSTS_INCLUDE_SUBDOMAINS", False)
SECURE_HSTS_PRELOAD = env_bool("DJANGO_HSTS_PRELOAD", False)
SILENCED_SYSTEM_CHECKS = [
    check
    for check, switched_on in (
        ("security.W004", SECURE_HSTS_SECONDS),
        ("security.W005", SECURE_HSTS_INCLUDE_SUBDOMAINS),
        ("security.W021", SECURE_HSTS_PRELOAD),
    )
    if not switched_on
]
HSTS_STARTUP_WARNING = True
SECURE_CONTENT_TYPE_NOSNIFF = True
X_FRAME_OPTIONS = "DENY"

# All mail goes to the outbox; the worker sends it through SMTP (EMAIL_HOST required) when
# OUTBOUND_EMAIL is on, and sends nothing when it is off or missing (fail closed).
OUTBOUND_EMAIL = env_bool("OUTBOUND_EMAIL", False)
MAILERS = {
    "default": {"BACKEND": OUTBOX_BACKEND},
    "outbound": outbound_mailer_from_env(host=None if OUTBOUND_EMAIL else "localhost"),
}

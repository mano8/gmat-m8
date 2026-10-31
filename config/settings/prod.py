"""Production settings. Every value that matters is required and validated: fail closed."""

from django.core.exceptions import ImproperlyConfigured

from config.env import (
    env_bool,
    env_fernet_keys,
    env_int,
    env_list,
    env_secret,
    non_production_fernet_keys,
)
from config.settings.base import *  # noqa: F403
from config.settings.base import OUTBOX_BACKEND, database_from_env, outbound_mailer_from_env

DEBUG = False
if env_bool("DJANGO_DEBUG", False):
    raise ImproperlyConfigured("DJANGO_DEBUG must not be enabled in production")

SECRET_KEY = env_secret("DJANGO_SECRET_KEY", min_length=50)

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

# TLS terminates at Caddy, the only proxy in front of the app. The internal health checks
# reach the app over plain HTTP, so they are exempt from the redirect.
SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
SECURE_SSL_REDIRECT = True
SECURE_REDIRECT_EXEMPT = [r"^healthz/$", r"^readyz/$"]
SESSION_COOKIE_SECURE = True
CSRF_COOKIE_SECURE = True
SECURE_HSTS_SECONDS = env_int("DJANGO_HSTS_SECONDS", 31536000, minimum=0)
SECURE_HSTS_INCLUDE_SUBDOMAINS = True
SECURE_HSTS_PRELOAD = env_bool("DJANGO_HSTS_PRELOAD", True)
SECURE_CONTENT_TYPE_NOSNIFF = True
X_FRAME_OPTIONS = "DENY"

# All mail goes to the outbox; the worker sends it through SMTP (EMAIL_HOST required) when
# OUTBOUND_EMAIL is on, and sends nothing when it is off or missing (fail closed).
OUTBOUND_EMAIL = env_bool("OUTBOUND_EMAIL", False)
MAILERS = {
    "default": {"BACKEND": OUTBOX_BACKEND},
    "outbound": outbound_mailer_from_env(host=None if OUTBOUND_EMAIL else "localhost"),
}

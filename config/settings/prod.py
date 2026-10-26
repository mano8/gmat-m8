"""Production settings. Every value that matters is required and validated: fail closed."""

from django.core.exceptions import ImproperlyConfigured

from config.env import env_bool, env_int, env_list, env_str
from config.settings.base import *  # noqa: F403

DEBUG = False
if env_bool("DJANGO_DEBUG", False):
    raise ImproperlyConfigured("DJANGO_DEBUG must not be enabled in production")

SECRET_KEY = env_str("DJANGO_SECRET_KEY", min_length=50)
if SECRET_KEY.startswith(("dev-", "test-", "build-", "change", "insecure")):
    raise ImproperlyConfigured("DJANGO_SECRET_KEY is a placeholder")

ALLOWED_HOSTS = env_list("DJANGO_ALLOWED_HOSTS")
if "*" in ALLOWED_HOSTS:
    raise ImproperlyConfigured("DJANGO_ALLOWED_HOSTS must not contain a wildcard")

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

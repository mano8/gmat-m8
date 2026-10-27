"""Local development settings. Never used by an image that serves traffic."""

from config.env import derived_fernet_key, env_bool, env_list, env_str
from config.settings.base import *  # noqa: F403
from config.settings.base import database_from_env

DEBUG = env_bool("DJANGO_DEBUG", True)
SECRET_KEY = env_str(
    "DJANGO_SECRET_KEY", "dev-only-insecure-key-never-use-outside-development-0000000"
)
ALLOWED_HOSTS = env_list("DJANGO_ALLOWED_HOSTS", ["localhost", "127.0.0.1", "[::1]"])
DATABASES = {"default": database_from_env()}
MFA_ENCRYPTION_KEYS = [derived_fernet_key("development")]

# `manage.py runserver` speaks plain HTTP: browsers refuse Secure and `__Host-` cookies
# there. The compose stack (HTTPS through Caddy) uses the production values.
SESSION_COOKIE_NAME = "sessionid"
CSRF_COOKIE_NAME = "csrftoken"
SESSION_COOKIE_SECURE = False
CSRF_COOKIE_SECURE = False
ACCOUNT_DEFAULT_HTTP_PROTOCOL = "http"
# No proxy in front of runserver: use the socket address.
ALLAUTH_TRUSTED_PROXY_COUNT = 0
MFA_WEBAUTHN_ALLOW_INSECURE_ORIGIN = True

"""Local development settings. Never used by an image that serves traffic."""

from config.env import derived_fernet_key, env_bool, env_list, env_str
from config.settings.base import *  # noqa: F403
from config.settings.base import OUTBOX_BACKEND, database_from_env, outbound_mailer_from_env

DEBUG = env_bool("DJANGO_DEBUG", True)
SECRET_KEY = env_str(
    "DJANGO_SECRET_KEY", "dev-only-insecure-key-never-use-outside-development-0000000"
)
ALLOWED_HOSTS = env_list("DJANGO_ALLOWED_HOSTS", ["localhost", "127.0.0.1", "[::1]"])
DATABASES = {"default": database_from_env()}
MFA_ENCRYPTION_KEYS = [derived_fernet_key("development")]
OUTBOX_ENCRYPTION_KEYS = [derived_fernet_key("outbox-development")]
# The worker delivers to a local SMTP sink: Mailpit in the compose dev overlay (EMAIL_HOST=
# mailpit), or anything listening on localhost:1025.
OUTBOUND_EMAIL = env_bool("OUTBOUND_EMAIL", True)
MAILERS = {
    "default": {"BACKEND": OUTBOX_BACKEND},
    "outbound": outbound_mailer_from_env(host="localhost", port=1025, use_tls=False),
}

# `manage.py runserver` speaks plain HTTP: browsers refuse Secure and `__Host-` cookies
# there. The compose stack (HTTPS through Caddy) uses the production values.
SESSION_COOKIE_NAME = "sessionid"
CSRF_COOKIE_NAME = "csrftoken"
LANGUAGE_COOKIE_NAME = "django_language"
LANGUAGE_COOKIE_SECURE = False
STORAGES = {
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"},
}
SESSION_COOKIE_SECURE = False
CSRF_COOKIE_SECURE = False
ACCOUNT_DEFAULT_HTTP_PROTOCOL = "http"
# No proxy in front of runserver: use the socket address.
ALLAUTH_TRUSTED_PROXY_COUNT = 0
MFA_WEBAUTHN_ALLOW_INSECURE_ORIGIN = True

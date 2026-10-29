"""Settings for the test suite. PostgreSQL only; the connection comes from ``DB_*``.

Hermetic database defaults are seeded here, not in a conftest: pytest-django and the
mypy plugin import this module before any conftest runs. ``setdefault`` lets a real
exported value (CI service container, a local PostgreSQL on another port) win. The
password is a throwaway for the test database only.
"""

import os

_TEST_ENV = {
    "DB_NAME": "gmat",
    "DB_USER": "gmat",
    "DB_PASSWORD": "gmat_test_pw",  # nosec B105
    "DB_HOST": "localhost",
    "DB_PORT": "5432",
}
for _key, _value in _TEST_ENV.items():
    os.environ.setdefault(_key, _value)

from config.env import derived_fernet_key  # noqa: E402
from config.settings.base import *  # noqa: E402, F403
from config.settings.base import database_from_env  # noqa: E402

DEBUG = False
SECRET_KEY = "test-only-secret-key-not-for-any-deployment-1234567890abcdefghijkl"  # noqa: S105  # nosec B105
ALLOWED_HOSTS = ["testserver", "localhost"]
DATABASES = {"default": database_from_env()}
PASSWORD_HASHERS = ["django.contrib.auth.hashers.MD5PasswordHasher"]
MFA_ENCRYPTION_KEYS = [derived_fernet_key("test")]
STORAGES = {
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"},
}
MAILERS = {"default": {"BACKEND": "django.core.mail.backends.locmem.EmailBackend"}}

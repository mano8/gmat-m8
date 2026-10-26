"""Settings for ``collectstatic`` during the image build: no database, no secrets."""

from config.settings.base import *  # noqa: F403

DEBUG = False
SECRET_KEY = "build-only-key-never-used-at-runtime"  # noqa: S105
ALLOWED_HOSTS: list[str] = []
# base defines no DATABASES, so no DB_* variable is read here.
DATABASES: dict[str, dict[str, object]] = {}

"""Local development settings. Never used by an image that serves traffic."""

from config.env import env_bool, env_list, env_str
from config.settings.base import *  # noqa: F403
from config.settings.base import database_from_env

DEBUG = env_bool("DJANGO_DEBUG", True)
SECRET_KEY = env_str(
    "DJANGO_SECRET_KEY", "dev-only-insecure-key-never-use-outside-development-0000000"
)
ALLOWED_HOSTS = env_list("DJANGO_ALLOWED_HOSTS", ["localhost", "127.0.0.1", "[::1]"])
DATABASES = {"default": database_from_env()}

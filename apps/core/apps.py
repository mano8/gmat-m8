from django.apps import AppConfig
from django.contrib.staticfiles.apps import StaticFilesConfig as _StaticFilesConfig


class CoreConfig(AppConfig):
    name = "apps.core"
    label = "core"
    default = True

    def ready(self) -> None:
        from apps.core import checks  # noqa: F401 - registers the system checks


class StaticFilesConfig(_StaticFilesConfig):
    """``collectstatic`` leaves out the Tailwind entry file and the vendored-code notes.

    ``static/src/app.css`` is a build input (``@import "tailwindcss"`` cannot be resolved by
    the hashing storage); only ``static/dist/app.css`` is served.
    """

    default = False
    ignore_patterns = [*_StaticFilesConfig.ignore_patterns, "src", "README.md"]

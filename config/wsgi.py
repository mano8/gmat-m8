import os

from django.core.wsgi import get_wsgi_application

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings.prod")
application = get_wsgi_application()

# After the app registry is ready. Only `web` loads this module, so a management command
# or the worker never repeats the warning.
from apps.accounts.security_log import warn_hsts_off  # noqa: E402

warn_hsts_off()

"""System check: an upload's enforced limits end well inside its lease (plan §5, F54)."""

from typing import Any

from django.conf import settings
from django.core.checks import Error, register

# Caddy's read_body timeout (120 s) plus the database steps' lock timeouts, with margin.
BODY_AND_DATABASE = 180


@register()
def lease_outlasts_the_limits(app_configs: Any = None, **kwargs: Any) -> list[Error]:
    needed = settings.ATTACHMENT_DECODE_TIMEOUT + BODY_AND_DATABASE
    if needed < settings.ATTACHMENT_LEASE:
        return []
    return [
        Error(
            f"ATTACHMENT_LEASE ({settings.ATTACHMENT_LEASE} s) must exceed the decoder's kill "
            f"plus the body and database limits ({needed} s): an expired lease would free a "
            "slot whose upload can still finish.",
            id="gmat.E020",
        )
    ]

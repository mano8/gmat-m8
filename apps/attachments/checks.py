"""System check: an upload's enforced limits end well inside its lease (plan §5, F54)."""

from typing import Any

from django.conf import settings
from django.core.checks import Error, register

# Caddy's read_body timeout (120 s) plus the database steps' lock timeouts, with margin.
BODY_AND_DATABASE = 180


@register()
def lease_outlasts_the_limits(app_configs: Any = None, **kwargs: Any) -> list[Error]:
    needed = settings.ATTACHMENT_DECODE_TIMEOUT + settings.ATTACHMENT_SCAN_TIMEOUT
    needed += BODY_AND_DATABASE
    if needed < settings.ATTACHMENT_LEASE:
        return []
    return [
        Error(
            f"ATTACHMENT_LEASE ({settings.ATTACHMENT_LEASE} s) must exceed the decoder's kill, "
            f"the scan and the body and database limits ({needed} s): an expired lease frees a "
            "slot whose upload can still finish.",
            id="gmat.E020",
        )
    ]


@register(deploy=True)
def uploads_go_through_their_services(app_configs: Any = None, **kwargs: Any) -> list[Error]:
    """Production scans every upload and decodes images apart from ``web`` (M8.3, ADR-0015)."""
    missing = [
        name
        for name in ("ATTACHMENT_SCAN_SOCKET", "ATTACHMENT_DECODER_SOCKET")
        if not getattr(settings, name)
    ]
    return [Error(f"{name} must be set in production", id="gmat.E022") for name in missing]

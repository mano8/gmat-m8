"""Request helpers shared by the rate limits and, later, the audit trail."""

import ipaddress

from django.conf import settings
from django.http import HttpRequest
from django.http.request import split_domain_port


def _parse_ip(value: str) -> str | None:
    value = value.strip()
    try:
        return str(ipaddress.ip_address(value))
    except ValueError:
        pass
    host, port = split_domain_port(value)
    if host and port:
        try:
            return str(ipaddress.ip_address(host.removeprefix("[").removesuffix("]")))
        except ValueError:
            return None
    return None


def client_ip(request: HttpRequest) -> str | None:
    """The client's IP address as the trusted proxy saw it (plan §5, "Client IP").

    Caddy is the only proxy (``ALLAUTH_TRUSTED_PROXY_COUNT = 1``) and replaces any
    ``X-Forwarded-For`` a client sends, so only the entries the trusted proxies appended,
    counted from the right, are believed. Anything further left is client-controlled and
    ignored. A request without the header (an internal health check) falls back to the
    socket address. ``None`` when no usable address remains; callers fail closed.
    """
    trusted = int(getattr(settings, "ALLAUTH_TRUSTED_PROXY_COUNT", 0))
    forwarded = request.META.get("HTTP_X_FORWARDED_FOR", "").strip()
    if trusted > 0 and forwarded:
        hops = [hop.strip() for hop in forwarded.split(",")]
        if len(hops) < trusted:
            return None
        return _parse_ip(hops[-trusted])
    remote = request.META.get("REMOTE_ADDR")
    return _parse_ip(remote) if remote else None


def is_htmx(request: HttpRequest) -> bool:
    """The request came from htmx (``django_htmx``'s ``request.htmx``): answer with a partial."""
    return bool(getattr(request, "htmx", False))

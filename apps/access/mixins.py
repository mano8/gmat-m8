"""The coarse view check and S7 for views (plan §3.6).

- ``require(request, capability, obj, read=..., methods=...)``: 405 for another method,
  404 for an object the user cannot see (``read``, default ``capability``), 403 for one
  they see but may not act on, and 403 when the capability is granted nowhere.
- ``@require_capability(capability)`` / ``CapabilityRequiredMixin``: login, then the
  capability somewhere (403). The row scope comes from the selectors' ``visible_to``.
- ``@mutating(capability)`` / ``MutatingViewMixin``: the same, POST only (405 for any
  other method, checked first). Only these views call services (F66).

These checks hide nothing on their own: every service authorizes again in its
transaction (``authorize_in_txn``).
"""

from collections.abc import Callable, Collection
from functools import wraps
from typing import Any, ClassVar

from django.contrib.auth.views import redirect_to_login
from django.db.models import Model
from django.http import HttpRequest, HttpResponseBase, HttpResponseNotAllowed
from django.views import View

from apps.access.exceptions import AccessDenied, MethodNotAllowed, NotInScope
from apps.access.middleware import access_for

type ViewFunction = Callable[..., HttpResponseBase]


def require(
    request: HttpRequest,
    capability: str,
    obj: Model | None = None,
    *,
    read: str | None = None,
    methods: Collection[str] | None = None,
) -> None:
    """S7: raise ``MethodNotAllowed`` (405), ``NotInScope`` (404) or ``AccessDenied`` (403)."""
    if methods is not None and request.method not in methods:
        raise MethodNotAllowed(tuple(methods))
    ctx = access_for(request)
    if obj is None:
        if not ctx.has(capability):
            raise AccessDenied(capability)
        return
    if not ctx.can(read or capability, obj):
        raise NotInScope(read or capability)
    if read is not None and not ctx.can(capability, obj):
        raise AccessDenied(capability)


def require_capability(
    capability: str, *, post_only: bool = False
) -> Callable[[ViewFunction], ViewFunction]:
    """Decorator: login, ``capability`` somewhere, and POST only when ``post_only``."""

    def decorator(view: ViewFunction) -> ViewFunction:
        @wraps(view)
        def wrapped(request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponseBase:
            if post_only and request.method != "POST":
                return HttpResponseNotAllowed(["POST"])
            if not request.user.is_authenticated:
                return redirect_to_login(request.get_full_path())
            require(request, capability)
            return view(request, *args, **kwargs)

        # Read by the acceptance matrix and the guardrails (M2.4).
        wrapped.required_capability = capability  # type: ignore[attr-defined]
        wrapped.post_only = post_only  # type: ignore[attr-defined]
        return wrapped

    return decorator


def mutating(capability: str) -> Callable[[ViewFunction], ViewFunction]:
    """Decorator for a view that calls a service: POST only, then ``require_capability``."""
    return require_capability(capability, post_only=True)


class CapabilityRequiredMixin(View):
    """Class-based ``require_capability``: set ``required_capability``."""

    required_capability: ClassVar[str]

    def dispatch(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponseBase:
        method = (request.method or "").lower()
        if method not in self.http_method_names:
            return self.http_method_not_allowed(request, *args, **kwargs)
        if not request.user.is_authenticated:
            return redirect_to_login(request.get_full_path())
        require(request, self.required_capability)
        return super().dispatch(request, *args, **kwargs)


class MutatingViewMixin(CapabilityRequiredMixin):
    """A view that calls a service accepts POST only (405 otherwise, F66)."""

    http_method_names = ["post"]

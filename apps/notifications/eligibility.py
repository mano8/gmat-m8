"""Who may still receive a message: checked by the worker in the claim transaction (plan §5).

- **Notifications**: each recipient must be active, not offboarded, and still hold the
  message's capability on its object (anywhere, for a message without an object), evaluated
  at the database clock of the claim through the recipient's own ``AccessContext``.
  Addresses come from the current user records.
- **Security emails**: the message's named rule must still hold for its target. M2.5
  registers ``account.active`` (allauth's mail: the account is active and still has this
  address); the invitation and email-change rules arrive with their tokens (M2.6).

Anything unknown (a rule nobody registered, an object type that no longer exists, a model
without a scoped queryset) makes nobody eligible: the message is suppressed, never sent.
"""

from collections.abc import Callable
from uuid import UUID

from django.apps import apps
from django.db.models import Model

from apps.access.context import AccessContext
from apps.access.scopes import ScopedQuerySet
from apps.accounts.models import User
from apps.notifications.models import EmailOutbox

type SecurityRule = Callable[[EmailOutbox], bool]

ACCOUNT_ACTIVE = "account.active"

_SECURITY_RULES: dict[str, SecurityRule] = {}


def register_security_rule(name: str, check: SecurityRule) -> None:
    """Register the eligibility check of a security email kind (from ``AppConfig.ready``)."""
    if _SECURITY_RULES.get(name, check) is not check:
        raise ValueError(f"Security rule {name!r} is already registered")
    _SECURITY_RULES[name] = check


def is_registered(name: str) -> bool:
    return name in _SECURITY_RULES


def object_reference(obj: Model) -> tuple[str, UUID]:
    """The stored form of an eligibility object: ``("app_label.model", pk)``."""
    return obj._meta.label_lower, obj.pk


def is_scoped(model: type[Model]) -> bool:
    """Whether a capability can be checked on rows of ``model`` (it has ``visible_to``)."""
    return isinstance(model._default_manager.all(), ScopedQuerySet)


def _load(label: str, pk: UUID | None) -> Model | None:
    if pk is None:
        return None
    try:
        model = apps.get_model(label)
    except LookupError, ValueError:
        return None
    found: Model | None = model._default_manager.filter(pk=pk).first()
    return found


def eligible_recipients(message: EmailOutbox) -> list[User]:
    """The notification's recipients who still qualify, in id order."""
    obj = None
    if message.eligibility_object_id is not None:
        obj = _load(message.eligibility_object_type, message.eligibility_object_id)
        if obj is None or not is_scoped(type(obj)):
            return []
    users = User.objects.filter(
        pk__in=message.recipient_users, is_active=True, offboarded_at__isnull=True
    ).order_by("pk")
    capability = message.eligibility_rule
    eligible = []
    for user in users:
        ctx = AccessContext.for_user(user)
        if ctx.has(capability) if obj is None else ctx.can(capability, obj):
            eligible.append(user)
    return eligible


def security_target_ok(message: EmailOutbox) -> bool:
    """The security email's rule still holds for its target."""
    rule = _SECURITY_RULES.get(message.eligibility_rule)
    return rule is not None and rule(message)


def _account_active(message: EmailOutbox) -> bool:
    if message.eligibility_object_type != User._meta.label_lower:
        return False
    user = _load(message.eligibility_object_type, message.eligibility_object_id)
    return (
        isinstance(user, User)
        and user.is_active
        and user.offboarded_at is None
        and user.email == message.recipient_address.lower()
    )


register_security_rule(ACCOUNT_ACTIVE, _account_active)

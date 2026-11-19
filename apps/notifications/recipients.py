"""Who a business notification goes to (plan §5, M7.1).

Recipients are chosen when the event is published, in the publisher's transaction, and are
checked again by the worker at the claim (``eligibility``): a user who lost the capability
by then receives nothing. Whether they want the message (their switch for the channel, their
own choice, an administrator's setting for them and the one for everyone; Q93) is decided
where it is queued (``outbox.queue_notification`` through ``resolution``) and again at the
claim.
"""

from collections.abc import Collection
from uuid import UUID

from django.db.models import Model

from apps.access.context import AccessContext
from apps.access.policy import valid_now
from apps.access.roles import ROLE_CAPABILITIES, Capability
from apps.accounts.models import User
from apps.organizations.models import Membership


def holders(capability: Capability, obj: Model, *, exclude: Collection[UUID] = ()) -> list[User]:
    """Active users who hold ``capability`` on ``obj`` now.

    Candidates are the holders of a current membership whose role carries the capability;
    each is then checked through their own ``AccessContext`` (scope, assignment, MFA-free:
    the same evaluation the worker repeats at the claim).
    """
    roles = [role for role, caps in ROLE_CAPABILITIES.items() if capability in caps]
    candidates = User.objects.filter(
        pk__in=Membership.objects.filter(valid_now(), role__in=roles).values("user_id"),
        is_active=True,
        offboarded_at__isnull=True,
    ).order_by("pk")
    if exclude:
        candidates = candidates.exclude(pk__in=exclude)
    return [user for user in candidates if AccessContext.for_user(user).can(capability, obj)]


def user_with(capability: Capability, obj: Model, user: User | None) -> list[User]:
    """``user`` alone, if active and holding ``capability`` on ``obj``."""
    if user is None or not user.is_active or user.offboarded_at is not None:
        return []
    if not AccessContext.for_user(user).can(capability, obj):
        return []
    return [user]

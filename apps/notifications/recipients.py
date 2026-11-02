"""Who a business notification goes to (plan §5, M7.1).

Recipients are chosen when the event is published, in the publisher's transaction, and are
checked again by the worker at the claim (``eligibility``): a user who lost the capability
by then receives nothing. A user who opted out of business email is never chosen.
"""

from uuid import UUID

from django.db.models import Model

from apps.access.context import AccessContext
from apps.access.policy import valid_now
from apps.access.roles import ROLE_CAPABILITIES, Capability
from apps.accounts.models import User
from apps.notifications.models import NotificationPreference
from apps.organizations.models import Membership


def opted_out(user_ids: set[UUID] | list[UUID]) -> set[UUID]:
    """The users among ``user_ids`` who turned business email off."""
    return set(
        NotificationPreference.objects.filter(user_id__in=user_ids, email_opt_out=True).values_list(
            "user_id", flat=True
        )
    )


def _active(users: list[User]) -> list[User]:
    skipped = opted_out([user.pk for user in users])
    return [user for user in users if user.pk not in skipped]


def holders(capability: Capability, obj: Model, *, exclude: UUID | None = None) -> list[User]:
    """Active users who hold ``capability`` on ``obj`` now, opted-out ones left out.

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
    if exclude is not None:
        candidates = candidates.exclude(pk=exclude)
    return _active(
        [user for user in candidates if AccessContext.for_user(user).can(capability, obj)]
    )


def user_with(capability: Capability, obj: Model, user: User | None) -> list[User]:
    """``user`` alone, if active, opted in and holding ``capability`` on ``obj``."""
    if user is None or not user.is_active or user.offboarded_at is not None:
        return []
    if not AccessContext.for_user(user).can(capability, obj):
        return []
    return _active([user])

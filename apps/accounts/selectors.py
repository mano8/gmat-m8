"""Reads for the people screens and the token pages (plan §3.4, §5).

Accounts, invitations and email changes are installation-wide; only ``org.manage``
(group administrators, S2) reads them through the people screens. The token pages read one
invitation or email change by the hash kept in the session: nothing else is reachable.
"""

from typing import Literal
from uuid import UUID

from django.db.models import Exists, OuterRef, Q, QuerySet
from django.db.models.functions import Now
from django.utils.translation import gettext_lazy as _

from apps.access.context import AccessContext
from apps.access.exceptions import NotInScope
from apps.access.policy import valid_now
from apps.access.roles import Capability
from apps.accounts.models import EmailChangeRequest, Invitation, User
from apps.accounts.tokens import same_hash
from apps.notifications import selectors as notification_selectors
from apps.organizations.models import Membership
from apps.projects.models import ProjectAssignment

DEFAULT_PER_PAGE = 25
PEOPLE_SORTS = {"name": ("full_name", "email"), "email": ("email",)}
PEOPLE_SORT_LABELS = {"name": _("Name"), "email": _("Email")}
INVITATION_SORTS = {"created": ("created_at",), "expires": ("expires_at",)}
INVITATION_SORT_LABELS = {"created": _("Created"), "expires": _("Expires")}
NO_EMAIL = "none"
EXPIRED, VALID = "expired", "valid"
STATUSES = ("active", "inactive")


def _admin(ctx: AccessContext) -> bool:
    return ctx.has(Capability.ORG_MANAGE)


def people(
    ctx: AccessContext,
    *,
    q: str = "",
    status: str = "",
    role: str = "",
    branch_id: UUID | None = None,
) -> QuerySet[User]:
    if not _admin(ctx):
        return User.objects.none()
    rows = User.objects.select_related("employer_company").order_by("email")
    if q:
        rows = rows.filter(Q(email__icontains=q) | Q(full_name__icontains=q))
    if status == "active":
        rows = rows.filter(is_active=True)
    elif status == "inactive":
        rows = rows.filter(is_active=False)
    if role or branch_id:
        rows = rows.filter(Exists(_holding(role, branch_id)))
    return rows


def _holding(role: str, branch_id: UUID | None) -> QuerySet[Membership]:
    """The user's valid memberships (outer ``pk``) of ``role`` and/or on ``branch_id``."""
    held = Membership.objects.filter(valid_now(), user_id=OuterRef("pk"))
    if role:
        held = held.filter(role=role)
    if branch_id:
        held = held.filter(branch_id=branch_id)
    return held


def person(ctx: AccessContext, user_id: UUID) -> User:
    found = people(ctx).filter(pk=user_id).first()
    if found is None:
        raise NotInScope(Capability.ORG_MANAGE)
    return found


def assignments_of(ctx: AccessContext, of: User) -> QuerySet[ProjectAssignment]:
    if not _admin(ctx):
        return ProjectAssignment.objects.none()
    return (
        ProjectAssignment.objects.filter(user=of)
        .select_related("project")
        .order_by("revoked_at", "-valid_from")
    )


def invitations(
    ctx: AccessContext,
    *,
    q: str = "",
    pending_only: bool = True,
    of: User | None = None,
    state: str = "",
    delivery: str = "",
) -> QuerySet[Invitation]:
    if not _admin(ctx):
        return Invitation.objects.none()
    rows = Invitation.objects.select_related("invited_by", "target_user").order_by("-created_at")
    if pending_only:
        rows = rows.filter(accepted_at__isnull=True, revoked_at__isnull=True)
    if q:
        rows = rows.filter(Q(email__icontains=q) | Q(full_name__icontains=q))
    if of is not None:
        rows = rows.filter(Q(target_user=of) | Q(email=of.email))
    if state:
        rows = (
            rows.filter(expires_at__lte=Now())
            if state == EXPIRED
            else rows.filter(expires_at__gt=Now())
        )
    if delivery:
        rows = _by_delivery(rows, delivery)
    return rows


def _by_delivery(rows: QuerySet[Invitation], delivery: str) -> QuerySet[Invitation]:
    """Invitations whose latest email is in state ``delivery`` (``NO_EMAIL``: none was sent)."""
    rows = notification_selectors.with_delivery_state(rows, notification_selectors.INVITATION)
    if delivery == NO_EMAIL:
        return rows.filter(Q(delivery_state__isnull=True))
    return rows.filter(Q(delivery_state=delivery))


def invitation(ctx: AccessContext, invitation_id: UUID) -> Invitation:
    found = invitations(ctx, pending_only=False).filter(pk=invitation_id).first()
    if found is None:
        raise NotInScope(Capability.ORG_MANAGE)
    return found


def email_changes(ctx: AccessContext, of: User) -> QuerySet[EmailChangeRequest]:
    if not _admin(ctx):
        return EmailChangeRequest.objects.none()
    return (
        EmailChangeRequest.objects.filter(user=of)
        .select_related("requested_by")
        .order_by("-created_at")
    )


def email_change(ctx: AccessContext, change_id: UUID) -> EmailChangeRequest:
    found = EmailChangeRequest.objects.filter(pk=change_id).first() if _admin(ctx) else None
    if found is None:
        raise NotInScope(Capability.ORG_MANAGE)
    return found


# --- token pages -------------------------------------------------------------------------


def invitation_by_hash(token_hash: str | None) -> Invitation | None:
    """The invitation a token page is about (any state; the page decides what to show)."""
    if not token_hash:
        return None
    found = Invitation.objects.select_related("target_user").filter(token_hash=token_hash).first()
    return found if found is not None and same_hash(found.token_hash, token_hash) else None


type EmailChangeAction = Literal["confirm", "cancel"]


def email_change_by_hash(
    token_hash: str | None,
) -> tuple[EmailChangeRequest, EmailChangeAction] | None:
    """The email change a token belongs to, and whether it confirms or cancels."""
    if not token_hash:
        return None
    found = (
        EmailChangeRequest.objects.select_related("user")
        .filter(Q(token_hash=token_hash) | Q(cancel_token_hash=token_hash))
        .first()
    )
    if found is None:
        return None
    # The query matched one of the two hashes; the comparison names which.
    return found, "confirm" if same_hash(found.token_hash, token_hash) else "cancel"

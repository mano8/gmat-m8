"""Account services: invitations, offboarding, email changes, the first administrator (plan §5).

Same write order as ``apps.organizations.services``: L0 locks (``AccessState`` ``FOR SHARE``,
then the target and actor ``User`` rows ``FOR NO KEY UPDATE`` in id order, then token rows),
``authorize_in_txn``, the idempotency record for a create that carries a key, validation, the
write, ``audit.record`` and ``events.publish``. Emails are queued in the same transaction as
sensitive security messages; their links carry the raw token, which nothing else keeps.

- Administrators (``org.manage``; a superuser for a staff account) create, resend and revoke
  invitations, offboard accounts and request or revoke email changes.
- Token principals (``apps.accounts.tokens``) accept an invitation, or confirm or cancel an
  email change, each exactly once.
- ``bootstrap_admin`` invites the first group administrator while none exists (F76).
- The worker applies confirmed email changes after the cooling-off
  (``apps.accounts.housekeeping``).
"""

from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass
from datetime import datetime
from typing import Any
from uuid import UUID

from allauth.account.models import EmailAddress
from allauth.mfa.models import Authenticator
from django.conf import settings
from django.core.exceptions import ValidationError
from django.template.loader import render_to_string
from django.urls import reverse
from django.utils.dateparse import parse_datetime
from django.utils.translation import gettext
from django.utils.translation import gettext_lazy as _

from apps.access.authorization import authorize_in_txn
from apps.access.exceptions import TokenRejected
from apps.access.generations import lock_access_state, lock_users
from apps.access.policy import platform_operator_flags, require_may_manage_account
from apps.access.principals import Principal, TokenPrincipal
from apps.access.roles import PRIVILEGED_ROLES, Capability, Role
from apps.accounts import tokens
from apps.accounts.models import EmailChangeRequest, Invitation, User
from apps.accounts.revocation import revoke_pending_tokens
from apps.audit.recorder import AuditIdentity, record
from apps.core import events
from apps.core.db import db_clock, db_now
from apps.core.idempotency import execute_once
from apps.core.locking import LockMode, LockOrder, LockSetChanged, transactional
from apps.notifications.outbox import expire_security_email, queue_security_email
from apps.organizations.grants import check_membership_target, elevate_privilege
from apps.organizations.models import Branch, Company, Membership
from apps.organizations.services import HEAD_OFFICE_CODE
from apps.projects.models import Project, ProjectAssignment

# Security-email rules (registered in ``apps.accounts.eligibility``).
INVITATION_PENDING = "invitation.pending"
EMAIL_CHANGE_CONFIRMABLE = "email_change.confirmable"
EMAIL_CHANGE_CANCELLABLE = "email_change.cancellable"

REASON_RESENT = "resent"
REASON_REPLACED = "replaced"
REASON_OFFBOARDED = "offboarded"


# --- grants ----------------------------------------------------------------------------


@dataclass(frozen=True)
class MembershipGrant:
    """A membership an invitation creates; it starts when the invitation is accepted."""

    role: str
    branch_id: UUID | None = None
    company_id: UUID | None = None
    valid_until: datetime | None = None


@dataclass(frozen=True)
class AssignmentGrant:
    """A project assignment an invitation creates (site managers and partners)."""

    project_id: UUID
    kind: str
    valid_until: datetime | None = None


def _grants_json(
    memberships: Sequence[MembershipGrant], assignments: Sequence[AssignmentGrant]
) -> dict[str, list[dict[str, Any]]]:
    def plain(item: MembershipGrant | AssignmentGrant) -> dict[str, Any]:
        return {
            key: (str(value) if isinstance(value, UUID) else value)
            if not isinstance(value, datetime)
            else value.isoformat()
            for key, value in asdict(item).items()
        }

    return {
        "memberships": [plain(item) for item in memberships],
        "assignments": [plain(item) for item in assignments],
    }


def _uuid(value: Any) -> UUID | None:
    return UUID(str(value)) if value else None


def _when(value: Any) -> datetime | None:
    return parse_datetime(value) if value else None


def parse_grants(grants: Mapping[str, Any]) -> tuple[list[MembershipGrant], list[AssignmentGrant]]:
    """The grants stored on an invitation."""
    memberships = [
        MembershipGrant(
            role=str(item["role"]),
            branch_id=_uuid(item.get("branch_id")),
            company_id=_uuid(item.get("company_id")),
            valid_until=_when(item.get("valid_until")),
        )
        for item in grants.get("memberships", [])
    ]
    assignments = [
        AssignmentGrant(
            project_id=UUID(str(item["project_id"])),
            kind=str(item["kind"]),
            valid_until=_when(item.get("valid_until")),
        )
        for item in grants.get("assignments", [])
    ]
    return memberships, assignments


def _unsaved_membership(grant: MembershipGrant, user: User | None, start: datetime) -> Membership:
    membership = Membership(
        role=grant.role,
        branch_id=grant.branch_id,
        company_id=grant.company_id,
        valid_from=start,
        valid_until=grant.valid_until,
    )
    if user is not None:
        membership.user = user
    return membership


def _active_project(project_id: UUID) -> Project:
    project = Project.objects.select_related("branch__company").filter(pk=project_id).first()
    if (
        project is None
        or project.status != Project.Status.ACTIVE
        or not (project.branch.is_active and project.branch.company.is_active)
    ):
        raise ValidationError(
            {"project": ValidationError(_("The project is not active."), code="inactive")}
        )
    return project


def _validate_grants(
    memberships: Sequence[MembershipGrant],
    assignments: Sequence[AssignmentGrant],
    employer_company_id: UUID | None,
) -> None:
    """Every grant has its role's shape and an active target (plan §5, step 2)."""
    if not memberships:
        raise ValidationError(_("An invitation grants at least one role."), code="no_grant")
    start = db_now()
    for grant in memberships:
        membership = _unsaved_membership(grant, None, start)
        membership.full_clean(exclude=["user", "revoked_by"])
        check_membership_target(membership, employer_company_id)
    for item in assignments:
        if item.kind not in ProjectAssignment.Kind.values:
            raise ValidationError({"kind": ValidationError(_("Unknown kind."), code="invalid")})
        _active_project(item.project_id)


# --- emails ----------------------------------------------------------------------------


def _absolute(path: str) -> str:
    return f"{str(settings.PUBLIC_BASE_URL).rstrip('/')}{path}"


def _subject(text: str) -> str:
    return f"{settings.ACCOUNT_EMAIL_SUBJECT_PREFIX}{text}"


def _queue_invitation_email(invitation: Invitation, raw_token: str) -> None:
    link = _absolute(reverse("invitations:link", args=[raw_token]))
    queue_security_email(
        address=invitation.email,
        target=invitation,
        rule=INVITATION_PENDING,
        subject=_subject(gettext("You are invited to gmat-m8")),
        body=render_to_string(
            "accounts/email/invitation.txt",
            {"invitation": invitation, "link": link, "hours": _hours(tokens.INVITATION_LIFETIME)},
        ),
        source_ref="invitation",
    )


def _hours(lifetime: Any) -> int:
    return int(lifetime.total_seconds() // 3600)


def _queue_email_change_emails(
    change: EmailChangeRequest, confirm_token: str, cancel_token: str
) -> None:
    context = {
        "change": change,
        "hours": _hours(tokens.EMAIL_CHANGE_LIFETIME),
        "cooling_off_hours": _hours(tokens.EMAIL_CHANGE_COOLING_OFF),
    }
    queue_security_email(
        address=change.new_email,
        target=change,
        rule=EMAIL_CHANGE_CONFIRMABLE,
        subject=_subject(gettext("Confirm your new email address")),
        body=render_to_string(
            "accounts/email/email_change_confirm.txt",
            {**context, "link": _absolute(reverse("email_change:link", args=[confirm_token]))},
        ),
        source_ref="email_change.confirm",
    )
    queue_security_email(
        address=change.user.email,
        target=change,
        rule=EMAIL_CHANGE_CANCELLABLE,
        subject=_subject(gettext("Your email address is about to change")),
        body=render_to_string(
            "accounts/email/email_change_notice.txt",
            {**context, "link": _absolute(reverse("email_change:link", args=[cancel_token]))},
        ),
        source_ref="email_change.cancel",
    )


# --- locks -----------------------------------------------------------------------------


def _lock_and_authorize(principal: Principal, target_id: UUID | None) -> User:
    """L0 for a change to ``target_id``'s account (or for a new one), then ``org.manage``."""
    locks = LockOrder()
    lock_access_state(locks)
    lock_users(
        locks,
        {principal.user_id} | ({target_id} if target_id else set()),
        mode=LockMode.NO_KEY_UPDATE,
    )
    actor = authorize_in_txn(principal, Capability.ORG_MANAGE, locks=locks)
    if target_id is not None:
        require_may_manage_account(actor, User.objects.get(pk=target_id))
    return actor


def _token_row(principal: TokenPrincipal) -> UUID:
    """The token row a persistent-token principal was built for."""
    if principal.token_id is None:
        raise TokenRejected("This principal carries no token row")
    return principal.token_id


def _normalize_email(email: str) -> str:
    return email.strip().lower()


def _snapshot(invitation: Invitation) -> dict[str, Any]:
    return {
        "email": invitation.email,
        "kind": invitation.kind,
        "target_user_id": invitation.target_user_id,
        "bootstrap": invitation.bootstrap,
        "platform_operator": invitation.platform_operator,
        "employer_company_id": invitation.employer_company_id,
        "grants": invitation.grants,
        "expires_at": invitation.expires_at,
    }


def _new_invitation(**fields: Any) -> tuple[Invitation, str]:
    """A saved invitation with a fresh token, created at the database's wall clock."""
    raw, digest = tokens.new_token()
    created = db_clock()
    invitation = Invitation(
        token_hash=digest,
        created_at=created,
        expires_at=created + tokens.INVITATION_LIFETIME,
        **fields,
    )
    invitation.full_clean()
    invitation.save()
    return invitation, raw


# --- invitations -----------------------------------------------------------------------


@transactional
def create_invitation(
    principal: Principal,
    *,
    email: str,
    memberships: Sequence[MembershipGrant],
    assignments: Sequence[AssignmentGrant] = (),
    full_name: str = "",
    employer_company_id: UUID | None = None,
    idempotency_key: str | None = None,
) -> Invitation:
    """Invite ``email``: a new account, or an existing one (plan §5, "Invitations").

    The kind is decided here. An active account accepts signed in as itself; an inactive
    one is a re-hire. Staff and superuser accounts are invited only by a superuser.
    ``employer_company_id`` is the employer of a new or returning account; an active
    account keeps its own.
    """
    email = _normalize_email(email)
    target_id = User.objects.filter(email=email).values_list("pk", flat=True).first()
    actor = _lock_and_authorize(principal, target_id)
    payload = {
        "email": email,
        "grants": _grants_json(memberships, assignments),
        "full_name": full_name,
        "employer_company_id": employer_company_id,
    }

    def execute() -> Invitation:
        target = User.objects.filter(email=email).first()
        if (target.pk if target else None) != target_id:
            raise LockSetChanged("An account with this email appeared")
        if Invitation.objects.filter(
            email=email, accepted_at__isnull=True, revoked_at__isnull=True
        ).exists():
            raise ValidationError(
                {
                    "email": ValidationError(
                        _("This address already has a pending invitation: resend or revoke it."),
                        code="pending",
                    )
                }
            )
        employer = employer_company_id
        if target is not None and target.is_active:
            if employer not in (None, target.employer_company_id):
                raise ValidationError(
                    {
                        "employer_company": ValidationError(
                            _("An active account keeps its employer."), code="employer"
                        )
                    }
                )
            employer = target.employer_company_id
        _validate_grants(memberships, assignments, employer)
        invitation, raw = _new_invitation(
            email=email,
            kind=Invitation.Kind.EXISTING_ACCOUNT if target else Invitation.Kind.NEW_ACCOUNT,
            target_user=target,
            invited_by=actor,
            full_name=full_name.strip(),
            employer_company_id=employer,
            grants=payload["grants"],
        )
        _queue_invitation_email(invitation, raw)
        record(
            principal.audit_identity,
            "invitation.created",
            obj=invitation,
            data=_snapshot(invitation),
        )
        events.publish("invitation.created", invitation_id=invitation.pk)
        return invitation

    return execute_once(
        actor_id=principal.user_id,
        key=idempotency_key,
        operation="invitation.create",
        payload=payload,
        execute=execute,
        replay=lambda _type, pk: Invitation.objects.get(pk=pk),
    )


def _pending_invitation(invitation_id: UUID) -> Invitation:
    invitation = Invitation.objects.select_for_update().get(pk=invitation_id)
    if not invitation.is_pending:
        raise ValidationError(_("This invitation is no longer pending."), code="not_pending")
    return invitation


def _revoke(invitation: Invitation, reason: str) -> None:
    invitation.revoked_at = db_clock()
    invitation.revoke_reason = reason
    invitation.save(update_fields=["revoked_at", "revoke_reason", "updated_at"])
    expire_security_email(targets=[invitation])


@transactional
def resend_invitation(principal: Principal, invitation_id: UUID) -> Invitation:
    """Issue a new token for a pending invitation; the old one dies (plan §5)."""
    target_id = Invitation.objects.values_list("target_user_id", flat=True).get(pk=invitation_id)
    actor = _lock_and_authorize(principal, target_id)
    old = _pending_invitation(invitation_id)
    if old.bootstrap:
        raise ValidationError(
            _("Run bootstrap_admin again to replace a first-administrator link."), code="bootstrap"
        )
    _revoke(old, REASON_RESENT)
    invitation, raw = _new_invitation(
        email=old.email,
        kind=old.kind,
        target_user_id=old.target_user_id,
        invited_by=actor,
        full_name=old.full_name,
        employer_company_id=old.employer_company_id,
        grants=old.grants,
    )
    _queue_invitation_email(invitation, raw)
    record(
        principal.audit_identity,
        "invitation.resent",
        obj=invitation,
        data={"replaces": old.pk, "expires_at": invitation.expires_at},
    )
    events.publish("invitation.resent", invitation_id=invitation.pk, replaces=old.pk)
    return invitation


@transactional
def revoke_invitation(principal: Principal, invitation_id: UUID, *, reason: str = "") -> Invitation:
    target_id = Invitation.objects.values_list("target_user_id", flat=True).get(pk=invitation_id)
    _lock_and_authorize(principal, target_id)
    invitation = _pending_invitation(invitation_id)
    _revoke(invitation, reason.strip()[:200])
    record(
        principal.audit_identity,
        "invitation.revoked",
        obj=invitation,
        data={"reason": invitation.revoke_reason},
    )
    events.publish("invitation.revoked", invitation_id=invitation.pk)
    return invitation


def _verified_email(user: User) -> None:
    """The account's one address, verified (plan §5: acceptance verifies it)."""
    EmailAddress.objects.filter(user=user).delete()
    EmailAddress.objects.create(user=user, email=user.email, primary=True, verified=True)


def _grant_on_acceptance(
    identity: AuditIdentity,
    user: User,
    memberships: Sequence[MembershipGrant],
    assignments: Sequence[AssignmentGrant],
) -> None:
    start = db_now()
    for grant in memberships:
        membership = _unsaved_membership(grant, user, start)
        membership.full_clean()
        check_membership_target(membership, user.employer_company_id)
        membership.save()
        if membership.role in PRIVILEGED_ROLES:
            elevate_privilege(user.pk)
        record(
            identity,
            "membership.granted",
            obj=membership,
            branch_id=membership.branch_id,
            data={
                "user_id": user.pk,
                "role": membership.role,
                "branch_id": membership.branch_id,
                "company_id": membership.company_id,
                "valid_from": membership.valid_from,
                "valid_until": membership.valid_until,
            },
        )
        events.publish("membership.granted", membership_id=membership.pk, user_id=user.pk)
    for item in assignments:
        project = _active_project(item.project_id)
        if ProjectAssignment.objects.filter(
            project=project, user=user, kind=item.kind, revoked_at__isnull=True
        ).exists():
            continue  # already shared with this account
        assignment = ProjectAssignment(
            project=project,
            user=user,
            kind=item.kind,
            valid_from=start,
            valid_until=item.valid_until,
        )
        assignment.full_clean()
        assignment.save()
        record(
            identity,
            "project_assignment.granted",
            obj=assignment,
            branch_id=project.branch_id,
            data={"project_id": project.pk, "user_id": user.pk, "kind": item.kind},
        )
        events.publish("project_assignment.granted", assignment_id=assignment.pk, user_id=user.pk)


@transactional
def accept_invitation(principal: TokenPrincipal, *, password: str | None = None) -> User:
    """Accept an invitation, once (plan §5, "Acceptance").

    - ``NEW_ACCOUNT``: creates the account with ``password``;
    - ``EXISTING_ACCOUNT``, active: only adds the grants, never touches credentials
      (``password`` must be ``None``);
    - ``EXISTING_ACCOUNT``, inactive (a re-hire): sets ``password``, removes the old
      authenticators and reactivates the account.

    The address is verified and the grants created; privileged roles move
    ``privilege_elevated_at``, so the account enrolls MFA before using them. A grant whose
    branch, company or project is no longer active refuses the whole acceptance.
    """
    target = authorize_in_txn(principal, tokens.ACCEPT_INVITATION, locks=LockOrder())
    invitation = Invitation.objects.get(pk=_token_row(principal))
    memberships, assignments = parse_grants(invitation.grants)
    reactivated = False
    if invitation.kind == Invitation.Kind.NEW_ACCOUNT:
        if not password:
            raise ValueError("A new account needs a password")
        user = User.objects.create_user(
            invitation.email,
            password,
            full_name=invitation.full_name,
            employer_company_id=invitation.employer_company_id,
            **platform_operator_flags(invitation.platform_operator),
        )
    elif target is not None and target.is_active:
        if password is not None:
            raise ValueError("Accepting for an active account never changes its password")
        user = target
    else:
        if not password or target is None:
            raise ValueError("A returning account needs a password")
        user = target
        Authenticator.objects.filter(user=user).delete()
        user.set_password(password)
        user.is_active = True
        user.offboarded_at = None
        if invitation.employer_company_id is not None:
            user.employer_company_id = invitation.employer_company_id
        if invitation.full_name:
            user.full_name = invitation.full_name
        user.save()
        reactivated = True
    _verified_email(user)
    identity = AuditIdentity(label=user.email, user_id=user.pk, via=principal.via, ip=principal.ip)
    try:
        _grant_on_acceptance(identity, user, memberships, assignments)
    except ValidationError as exc:
        raise TokenRejected("A granted branch, company or project is no longer active") from exc
    invitation.accepted_at = db_clock()
    invitation.target_user = user
    invitation.save(update_fields=["accepted_at", "target_user", "updated_at"])
    record(
        identity,
        "invitation.accepted",
        obj=invitation,
        data={"kind": invitation.kind, "user_id": user.pk, "reactivated": reactivated},
    )
    events.publish("invitation.accepted", invitation_id=invitation.pk, user_id=user.pk)
    return user


# --- offboarding -----------------------------------------------------------------------


@transactional
def offboard_user(principal: Principal, user_id: UUID) -> User:
    """Offboard an account in one transaction (plan §5, "Offboarding", F41, F71).

    Deactivates it (``offboarded_at``; the triggers bump ``authz_generation`` and
    ``session_epoch``), revokes every membership and assignment, every pending invitation and
    email change for it or initiated by it, and expires its unsent security emails. An
    administrator cannot offboard their own account.
    """
    actor = _lock_and_authorize(principal, user_id)
    if actor.pk == user_id:
        raise ValidationError(_("You cannot offboard your own account."), code="self")
    user = User.objects.get(pk=user_id)
    if user.offboarded_at is not None:
        raise ValidationError(_("This account is already offboarded."), code="offboarded")
    now = db_clock()
    user.is_active = False
    user.offboarded_at = now
    user.save(update_fields=["is_active", "offboarded_at"])
    memberships = Membership.objects.filter(user=user, revoked_at__isnull=True).update(
        revoked_at=now, revoked_by=actor
    )
    assignments = ProjectAssignment.objects.filter(user=user, revoked_at__isnull=True).update(
        revoked_at=now, revoked_by=actor
    )
    revoked = revoke_pending_tokens(
        reason=REASON_OFFBOARDED, user_id=user.pk, email=user.email, initiated_by=user.pk
    )
    expired = expire_security_email(addresses=[user.email], targets=[user])
    record(
        principal.audit_identity,
        "user.offboarded",
        obj=user,
        data={
            "memberships_revoked": memberships,
            "assignments_revoked": assignments,
            "invitations_revoked": [item.pk for item in revoked.invitations],
            "email_changes_revoked": [item.pk for item in revoked.email_changes],
            "emails_expired": expired,
        },
    )
    events.publish("user.offboarded", user_id=user.pk)
    return user


# --- email changes ---------------------------------------------------------------------


@transactional
def request_email_change(
    principal: Principal, user_id: UUID, *, new_email: str
) -> EmailChangeRequest:
    """Start an email change: the new address confirms, the old one may cancel (Q26, Q28).

    A previous open request of the account is replaced.
    """
    actor = _lock_and_authorize(principal, user_id)
    user = User.objects.get(pk=user_id)
    new_email = _normalize_email(new_email)
    if not user.is_active or user.offboarded_at is not None:
        raise ValidationError(_("This account is inactive."), code="inactive")
    if new_email == user.email:
        raise ValidationError(
            {"new_email": ValidationError(_("This is the current address."), code="same")}
        )
    if User.objects.filter(email=new_email).exists():
        raise ValidationError(
            {"new_email": ValidationError(_("Another account uses this address."), code="taken")}
        )
    previous = list(
        EmailChangeRequest.objects.select_for_update().filter(
            user=user, applied_at__isnull=True, cancelled_at__isnull=True, revoked_at__isnull=True
        )
    )
    now = db_now()
    for item in previous:
        item.revoked_at = now
        item.revoke_reason = REASON_REPLACED
        item.save(update_fields=["revoked_at", "revoke_reason", "updated_at"])
    expire_security_email(targets=previous)
    confirm_raw, confirm_hash = tokens.new_token()
    cancel_raw, cancel_hash = tokens.new_token()
    change = EmailChangeRequest(
        user=user,
        new_email=new_email,
        requested_by=actor,
        token_hash=confirm_hash,
        cancel_token_hash=cancel_hash,
        expires_at=now + tokens.EMAIL_CHANGE_LIFETIME,
    )
    change.full_clean()
    change.save()
    _queue_email_change_emails(change, confirm_raw, cancel_raw)
    record(
        principal.audit_identity,
        "email_change.requested",
        obj=change,
        data={
            "user_id": user.pk,
            "new_email": new_email,
            "replaces": [item.pk for item in previous],
        },
    )
    events.publish("email_change.requested", change_id=change.pk, user_id=user.pk)
    return change


@transactional
def revoke_email_change(
    principal: Principal, change_id: UUID, *, reason: str = ""
) -> EmailChangeRequest:
    user_id = EmailChangeRequest.objects.values_list("user_id", flat=True).get(pk=change_id)
    _lock_and_authorize(principal, user_id)
    change = EmailChangeRequest.objects.select_for_update().get(pk=change_id)
    if not change.is_open:
        raise ValidationError(_("This email change is no longer open."), code="closed")
    change.revoked_at = db_now()
    change.revoke_reason = reason.strip()[:200]
    change.save(update_fields=["revoked_at", "revoke_reason", "updated_at"])
    expire_security_email(targets=[change])
    record(
        principal.audit_identity,
        "email_change.revoked",
        obj=change,
        data={"reason": change.revoke_reason},
    )
    events.publish("email_change.revoked", change_id=change.pk)
    return change


@transactional
def confirm_email_change(principal: TokenPrincipal) -> EmailChangeRequest:
    """The new address confirms; the cooling-off starts (the worker applies it later)."""
    authorize_in_txn(principal, tokens.CONFIRM_EMAIL_CHANGE, locks=LockOrder())
    change = EmailChangeRequest.objects.get(pk=_token_row(principal))
    now = db_now()
    change.confirmed_at = now
    change.apply_after = now + tokens.EMAIL_CHANGE_COOLING_OFF
    change.save(update_fields=["confirmed_at", "apply_after", "updated_at"])
    record(
        principal.audit_identity,
        "email_change.confirmed",
        obj=change,
        data={"apply_after": change.apply_after},
    )
    events.publish("email_change.confirmed", change_id=change.pk)
    return change


@transactional
def cancel_email_change(principal: TokenPrincipal) -> EmailChangeRequest:
    """The old address cancels, any time before the change is applied."""
    authorize_in_txn(principal, tokens.CANCEL_EMAIL_CHANGE, locks=LockOrder())
    change = EmailChangeRequest.objects.get(pk=_token_row(principal))
    change.cancelled_at = db_now()
    change.save(update_fields=["cancelled_at", "updated_at"])
    expire_security_email(targets=[change])
    record(principal.audit_identity, "email_change.cancelled", obj=change)
    events.publish("email_change.cancelled", change_id=change.pk)
    return change


# --- the first administrator ------------------------------------------------------------


@dataclass(frozen=True)
class Bootstrap:
    invitation: Invitation
    link: str  # the one-time setup link, for the operator's terminal only (Q29)
    company_created: bool


@transactional
def bootstrap_admin(
    principal: TokenPrincipal,
    *,
    company_code: str,
    company_name: str = "",
    platform_operator: bool = False,
) -> Bootstrap:
    """Invite the first ``GROUP_ADMIN`` (F76): only while no active one exists.

    Creates the company and its head office if they are missing. Running it again before
    the link is used replaces the previous link.
    """
    locks = LockOrder()
    # Serializes bootstraps with each other and with every organization change.
    lock_access_state(locks, mode=LockMode.UPDATE)
    authorize_in_txn(principal, tokens.BOOTSTRAP_ADMIN, locks=locks)
    email = _normalize_email(principal.target_email)
    if User.objects.filter(email=email).exists():
        raise ValidationError(
            {"email": ValidationError(_("An account with this email exists."), code="exists")}
        )
    identity = principal.audit_identity
    company = Company.objects.filter(code=company_code).first()
    created = company is None
    if company is None:
        company = Company(code=company_code, legal_name=company_name or company_code)
        company.full_clean()
        company.save()
        head_office = Branch(company=company, code=HEAD_OFFICE_CODE, name=company.legal_name)
        head_office.full_clean()
        head_office.save()
        record(identity, "company.created", obj=company, data={"code": company.code})
        record(
            identity,
            "branch.created",
            obj=head_office,
            branch_id=head_office.pk,
            data={"company_id": company.pk, "code": head_office.code},
        )
    elif not company.is_active:
        raise ValidationError(
            {"company": ValidationError(_("The company is inactive."), code="inactive")}
        )
    for previous in Invitation.objects.select_for_update().filter(
        bootstrap=True, accepted_at__isnull=True, revoked_at__isnull=True
    ):
        _revoke(previous, REASON_REPLACED)
    invitation, raw = _new_invitation(
        email=email,
        kind=Invitation.Kind.NEW_ACCOUNT,
        bootstrap=True,
        platform_operator=platform_operator,
        employer_company=company,
        grants=_grants_json([MembershipGrant(role=Role.GROUP_ADMIN)], []),
    )
    record(identity, "invitation.created", obj=invitation, data=_snapshot(invitation))
    events.publish("invitation.created", invitation_id=invitation.pk)
    return Bootstrap(invitation, _absolute(reverse("invitations:link", args=[raw])), created)

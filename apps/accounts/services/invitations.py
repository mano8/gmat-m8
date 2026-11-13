"""Invitations: created, resent, revoked and accepted, and the first administrator's (F76)."""

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from allauth.account.models import EmailAddress
from allauth.mfa.models import Authenticator
from django.core.exceptions import ValidationError
from django.template.loader import render_to_string
from django.urls import reverse
from django.utils.translation import gettext
from django.utils.translation import gettext_lazy as _

from apps.access.authorization import authorize_in_txn
from apps.access.exceptions import TokenRejected
from apps.access.generations import lock_access_state
from apps.access.policy import platform_operator_flags
from apps.access.principals import Principal, TokenPrincipal
from apps.access.roles import Role
from apps.accounts import tokens
from apps.accounts.models import Invitation, User
from apps.accounts.services.common import (
    INVITATION_MAIL,
    INVITATION_PENDING,
    REASON_REPLACED,
    REASON_RESENT,
    _absolute,
    _hours,
    _lock_and_authorize,
    _normalize_email,
    _subject,
    _token_row,
)
from apps.accounts.services.grants import (
    AssignmentGrant,
    MembershipGrant,
    _grant_on_acceptance,
    _grants_json,
    _validate_grants,
    parse_grants,
)
from apps.audit.recorder import AuditIdentity, record
from apps.core import events
from apps.core.db import db_clock
from apps.core.idempotency import execute_once
from apps.core.locking import LockMode, LockOrder, LockSetChanged, transactional
from apps.notifications.outbox import expire_security_email, queue_security_email
from apps.organizations.models import Branch, Company
from apps.organizations.services import HEAD_OFFICE_CODE


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
        source_ref=INVITATION_MAIL,
    )


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


@transactional
def create_invitation(
    principal: Principal,
    *,
    email: str,
    memberships: Sequence[MembershipGrant],
    full_name: str,
    assignments: Sequence[AssignmentGrant] = (),
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
    _require_name(full_name)
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


def _new_account(invitation: Invitation, password: str | None) -> User:
    if not password:
        raise ValueError("A new account needs a password")
    return User.objects.create_user(
        invitation.email,
        password,
        full_name=invitation.full_name,
        employer_company_id=invitation.employer_company_id,
        **platform_operator_flags(invitation.platform_operator),
    )


def _returning_account(invitation: Invitation, target: User | None, password: str | None) -> User:
    if not password or target is None:
        raise ValueError("A returning account needs a password")
    Authenticator.objects.filter(user=target).delete()
    target.set_password(password)
    target.is_active = True
    target.offboarded_at = None
    if invitation.employer_company_id is not None:
        target.employer_company_id = invitation.employer_company_id
    if invitation.full_name:
        target.full_name = invitation.full_name
    target.save()
    return target


def _accepting_account(
    invitation: Invitation, target: User | None, password: str | None
) -> tuple[User, bool]:
    """The account that accepts, and whether acceptance reactivated it."""
    if invitation.kind == Invitation.Kind.NEW_ACCOUNT:
        return _new_account(invitation, password), False
    if target is not None and target.is_active:
        if password is not None:
            raise ValueError("Accepting for an active account never changes its password")
        if not target.full_name.strip() and invitation.full_name:
            # An account from before names were required takes the one it was invited
            # with; a named account keeps its own (ADR-0029).
            target.full_name = invitation.full_name
            target.save(update_fields=["full_name"])
        return target, False
    return _returning_account(invitation, target, password), True


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
    name_before = target.full_name if target is not None else None
    user, reactivated = _accepting_account(invitation, target, password)
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
        data={
            "kind": invitation.kind,
            "user_id": user.pk,
            "reactivated": reactivated,
            # An existing account named by the invitation (ADR-0029).
            "named": name_before is not None and user.full_name != name_before,
        },
    )
    events.publish("invitation.accepted", invitation_id=invitation.pk, user_id=user.pk)
    return user


# --- the first administrator ------------------------------------------------------------


@dataclass(frozen=True)
class Bootstrap:
    invitation: Invitation
    link: str  # the one-time setup link, for the operator's terminal only (Q29)
    company_created: bool


def _require_name(full_name: str) -> None:
    """Everyone the application shows is shown by name, never by address (ADR-0029)."""
    if not full_name.strip():
        raise ValidationError(
            {"full_name": ValidationError(_("Give the person's full name."), code="required")}
        )


@transactional
def bootstrap_admin(
    principal: TokenPrincipal,
    *,
    company_code: str,
    full_name: str,
    company_name: str = "",
    platform_operator: bool = False,
) -> Bootstrap:
    """Invite the first ``GROUP_ADMIN`` (F76): only while no active one exists.

    Creates the company and its head office if they are missing. Running it again before
    the link is used replaces the previous link. The administrator is named (ADR-0029).
    """
    locks = LockOrder()
    # Serializes bootstraps with each other and with every organization change.
    lock_access_state(locks, mode=LockMode.UPDATE)
    authorize_in_txn(principal, tokens.BOOTSTRAP_ADMIN, locks=locks)
    _require_name(full_name)
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
        full_name=full_name.strip(),
        platform_operator=platform_operator,
        employer_company=company,
        grants=_grants_json([MembershipGrant(role=Role.GROUP_ADMIN)], []),
    )
    record(identity, "invitation.created", obj=invitation, data=_snapshot(invitation))
    events.publish("invitation.created", invitation_id=invitation.pk)
    return Bootstrap(invitation, _absolute(reverse("invitations:link", args=[raw])), created)

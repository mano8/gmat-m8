"""Organization services: companies, joint ventures, branches, memberships (plan §3.3, §3.6).

Each service runs in one transaction with ``lock_timeout`` through the shared retry
wrapper, in the write order of plan §4.7:

1. L0 locks. A change to the organization locks ``AccessState`` ``FOR UPDATE``, which
   serializes it with every write (they hold it ``FOR SHARE``) and with every other
   organization change. A change to a user's authority locks ``AccessState`` ``FOR SHARE``
   and then the target and actor ``User`` rows ``FOR NO KEY UPDATE`` in id order.
2. ``authorize_in_txn`` (``org.manage``), and ``require_may_manage_account`` for a user.
   Group administrators alone hold ``org.manage``, everywhere (S2).
3. The idempotency record, for a create that carries a key.
4. Validation (``full_clean`` plus the cross-row rules), the write, ``audit.record`` and
   ``events.publish``.

The generation counters are bumped by database triggers (``access.0002``), not here.
"""

from datetime import datetime
from decimal import Decimal
from typing import Any
from uuid import UUID

from django.core.exceptions import ValidationError
from django.db.models import F, Model
from django.db.models.functions import Coalesce, Greatest, Now
from django.utils.translation import gettext_lazy as _

from apps.access.authorization import authorize_in_txn
from apps.access.generations import lock_access_state, lock_users
from apps.access.policy import require_may_manage_account
from apps.access.principals import Principal
from apps.access.roles import COMPANY_ROLES, PRIVILEGED_ROLES, Capability, Role
from apps.accounts.models import User
from apps.audit.recorder import record
from apps.core import events
from apps.core.changes import apply_changes
from apps.core.db import db_now
from apps.core.idempotency import execute_once
from apps.core.locking import LockMode, LockOrder, transactional
from apps.locations.models import Location
from apps.organizations.deactivation import DeactivationRefused, open_documents
from apps.organizations.models import Branch, Company, JointVentureMember, Membership

HEAD_OFFICE_CODE = "HQ"
COMPANY_FIELDS = frozenset({"legal_name", "tax_id", "kind", "is_active"})
BRANCH_FIELDS = frozenset(
    {
        "name",
        "city",
        "address",
        "managed_by_id",
        "allow_self_approval",
        "default_warehouse_id",
        "is_active",
    }
)


def _org_change(principal: Principal) -> User:
    """L0 for an organization change, then the authorization; return the actor."""
    locks = LockOrder()
    lock_access_state(locks, mode=LockMode.UPDATE)
    return authorize_in_txn(principal, Capability.ORG_MANAGE, locks=locks)


def lock_for_user_change(
    principal: Principal,
    user_id: UUID,
    capability: str,
    *objects: Model,
    read: str | None = None,
) -> User:
    """L0 for a change to ``user_id``'s authority, then the authorization; return the actor.

    ``objects`` must be in the actor's scope for ``capability`` (the project of an
    assignment); with ``read``, one the actor cannot see is ``NotInScope``. Also used by
    the project-assignment services. Raises ``User.DoesNotExist``.
    """
    locks = LockOrder()
    lock_access_state(locks)
    lock_users(locks, {user_id, principal.user_id}, mode=LockMode.NO_KEY_UPDATE)
    actor = authorize_in_txn(principal, capability, *objects, locks=locks, read=read)
    require_may_manage_account(actor, User.objects.get(pk=user_id))
    return actor


def _elevate_privilege(user_id: UUID) -> None:
    """Granting a privileged role moves ``privilege_elevated_at`` to now (plan §3.6).

    The database clock of the grant; never moved back. Sessions must complete MFA after it
    before they use any privileged grant, including one that starts later: an elevation in
    the future would refuse every MFA done before then, even a step-up, and lock an
    already privileged user out until that time.
    """
    User.objects.filter(pk=user_id).update(
        privilege_elevated_at=Greatest(Coalesce(F("privilege_elevated_at"), Now()), Now())
    )


def _snapshot(obj: Any, fields: tuple[str, ...]) -> dict[str, Any]:
    return {name: getattr(obj, name) for name in fields}


# --- companies ----------------------------------------------------------------------


@transactional
def create_company(
    principal: Principal,
    *,
    code: str,
    legal_name: str,
    kind: str = Company.Kind.STANDARD,
    tax_id: str | None = None,
    idempotency_key: str | None = None,
) -> Company:
    """Create a company and its head-office branch (plan §3.3)."""
    _org_change(principal)
    payload = {"code": code, "legal_name": legal_name, "kind": kind, "tax_id": tax_id}

    def execute() -> Company:
        company = Company(code=code, legal_name=legal_name, kind=kind, tax_id=tax_id)
        company.full_clean()
        company.save()
        head_office = Branch(company=company, code=HEAD_OFFICE_CODE, name=legal_name)
        head_office.full_clean()
        head_office.save()
        identity = principal.audit_identity
        record(
            identity,
            "company.created",
            obj=company,
            data=_snapshot(company, ("code", "legal_name", "kind", "tax_id")),
        )
        record(
            identity,
            "branch.created",
            obj=head_office,
            branch_id=head_office.pk,
            data=_snapshot(head_office, ("company_id", "code", "name")),
        )
        events.publish("company.created", company_id=company.pk, head_office_id=head_office.pk)
        return company

    return execute_once(
        actor_id=principal.user_id,
        key=idempotency_key,
        operation="company.create",
        payload=payload,
        execute=execute,
        replay=lambda _type, pk: Company.objects.get(pk=pk),
    )


@transactional
def update_company(principal: Principal, company_id: UUID, **changes: Any) -> Company:
    """Change a company's data or ``is_active``; ``code`` is fixed (plan §4.2).

    Deactivation is refused while a branch of the company has open documents (F62).
    A joint venture with members cannot become ``STANDARD``.
    """
    _org_change(principal)
    company = Company.objects.get(pk=company_id)
    diff = apply_changes(company, changes, COMPANY_FIELDS)
    if not diff:
        return company
    company.full_clean()
    if (
        "kind" in diff
        and company.kind != Company.Kind.JOINT_VENTURE
        and JointVentureMember.objects.filter(venture_id=company.pk).exists()
    ):
        raise ValidationError(
            {
                "kind": ValidationError(
                    _("A joint venture with members must stay a joint venture."),
                    code="venture_has_members",
                )
            }
        )
    if diff.get("is_active", {}).get("to") is False:
        branch_ids = list(company.branches.values_list("pk", flat=True))
        blocking = open_documents(branch_ids)
        if blocking:
            raise DeactivationRefused(blocking)
    company.save()
    record(principal.audit_identity, "company.updated", obj=company, data=diff)
    events.publish("company.updated", company_id=company.pk, fields=sorted(diff))
    return company


# --- joint ventures -----------------------------------------------------------------


@transactional
def add_joint_venture_member(
    principal: Principal,
    *,
    venture_id: UUID,
    member_id: UUID,
    share_pct: Decimal | None = None,
    idempotency_key: str | None = None,
) -> JointVentureMember:
    """Record ``member`` as a member of ``venture`` (informational, plan §3.3)."""
    _org_change(principal)
    payload = {"venture_id": venture_id, "member_id": member_id, "share_pct": share_pct}

    def execute() -> JointVentureMember:
        venture = Company.objects.get(pk=venture_id)
        if venture.kind != Company.Kind.JOINT_VENTURE:
            raise ValidationError(
                {
                    "venture": ValidationError(
                        _("Only a joint venture has members."), code="not_a_venture"
                    )
                }
            )
        member = JointVentureMember(venture=venture, member_id=member_id, share_pct=share_pct)
        member.full_clean()
        member.save()
        record(
            principal.audit_identity,
            "joint_venture_member.added",
            obj=member,
            data=_snapshot(member, ("venture_id", "member_id", "share_pct")),
        )
        events.publish("joint_venture_member.added", member_id=member.pk)
        return member

    return execute_once(
        actor_id=principal.user_id,
        key=idempotency_key,
        operation="joint_venture_member.add",
        payload=payload,
        execute=execute,
        replay=lambda _type, pk: JointVentureMember.objects.get(pk=pk),
    )


@transactional
def update_joint_venture_member(
    principal: Principal, member_id: UUID, *, share_pct: Decimal | None
) -> JointVentureMember:
    _org_change(principal)
    member = JointVentureMember.objects.get(pk=member_id)
    diff = apply_changes(member, {"share_pct": share_pct}, {"share_pct"})
    if diff:
        member.full_clean()
        member.save()
        record(principal.audit_identity, "joint_venture_member.updated", obj=member, data=diff)
        events.publish("joint_venture_member.updated", member_id=member.pk)
    return member


# --- branches -----------------------------------------------------------------------


def _check_default_warehouse(branch: Branch) -> None:
    if branch.default_warehouse_id is None:
        return
    if not Location.objects.filter(
        pk=branch.default_warehouse_id,
        branch_id=branch.pk,
        kind=Location.Kind.WAREHOUSE,
        is_active=True,
    ).exists():
        raise ValidationError(
            {
                "default_warehouse": ValidationError(
                    _("Choose an active warehouse of this branch."),
                    code="invalid_default_warehouse",
                )
            }
        )


@transactional
def create_branch(
    principal: Principal,
    *,
    company_id: UUID,
    code: str,
    name: str,
    city: str = "",
    address: str = "",
    managed_by_id: UUID | None = None,
    allow_self_approval: bool = False,
    idempotency_key: str | None = None,
) -> Branch:
    """Create a branch of an active company. Its warehouses come after it (Q3)."""
    _org_change(principal)
    payload = {
        "company_id": company_id,
        "code": code,
        "name": name,
        "city": city,
        "address": address,
        "managed_by_id": managed_by_id,
        "allow_self_approval": allow_self_approval,
    }

    def execute() -> Branch:
        company = Company.objects.get(pk=company_id)
        if not company.is_active:
            raise ValidationError(
                {"company": ValidationError(_("The company is inactive."), code="inactive_company")}
            )
        branch = Branch(company=company, **{k: v for k, v in payload.items() if k != "company_id"})
        branch.full_clean()
        branch.save()
        record(
            principal.audit_identity,
            "branch.created",
            obj=branch,
            branch_id=branch.pk,
            data=_snapshot(
                branch,
                ("company_id", "code", "name", "managed_by_id", "allow_self_approval"),
            ),
        )
        events.publish("branch.created", branch_id=branch.pk)
        return branch

    return execute_once(
        actor_id=principal.user_id,
        key=idempotency_key,
        operation="branch.create",
        payload=payload,
        execute=execute,
        replay=lambda _type, pk: Branch.objects.get(pk=pk),
    )


@transactional
def update_branch(principal: Principal, branch_id: UUID, **changes: Any) -> Branch:
    """Change a branch's data, ``managed_by``, default warehouse or ``is_active``.

    ``company`` and ``code`` are fixed. ``managed_by`` stays one level deep (``clean`` and
    the trigger). Deactivation is refused while the branch has open documents (F62).
    """
    _org_change(principal)
    branch = Branch.objects.get(pk=branch_id)
    diff = apply_changes(branch, changes, BRANCH_FIELDS)
    if not diff:
        return branch
    branch.full_clean()
    _check_default_warehouse(branch)
    if diff.get("is_active", {}).get("to") is False:
        blocking = open_documents([branch.pk])
        if blocking:
            raise DeactivationRefused(blocking)
    branch.save()
    record(principal.audit_identity, "branch.updated", obj=branch, branch_id=branch.pk, data=diff)
    events.publish("branch.updated", branch_id=branch.pk, fields=sorted(diff))
    return branch


# --- memberships and employer -------------------------------------------------------


def _check_membership_target(membership: Membership, user: User) -> None:
    """The granted branch or company is active; a partner's company is their employer."""
    if membership.branch_id is not None:
        branch = Branch.objects.select_related("company").get(pk=membership.branch_id)
        if not (branch.is_active and branch.company.is_active):
            raise ValidationError(
                {"branch": ValidationError(_("The branch is inactive."), code="inactive")}
            )
    if membership.company_id is not None:
        if not Company.objects.filter(pk=membership.company_id, is_active=True).exists():
            raise ValidationError(
                {"company": ValidationError(_("The company is inactive."), code="inactive")}
            )
        if membership.role in COMPANY_ROLES and membership.company_id != user.employer_company_id:
            raise ValidationError(
                {
                    "company": ValidationError(
                        _("A partner's membership is on the company that employs them."),
                        code="not_employer",
                    )
                }
            )


@transactional
def grant_membership(
    principal: Principal,
    *,
    user_id: UUID,
    role: str,
    branch_id: UUID | None = None,
    company_id: UUID | None = None,
    valid_from: datetime | None = None,
    valid_until: datetime | None = None,
    idempotency_key: str | None = None,
) -> Membership:
    """Grant ``role`` to a user; ``valid_from`` defaults to the database clock (S8)."""
    actor = lock_for_user_change(principal, user_id, Capability.ORG_MANAGE)
    payload = {
        "user_id": user_id,
        "role": role,
        "branch_id": branch_id,
        "company_id": company_id,
        "valid_from": valid_from,
        "valid_until": valid_until,
    }

    def execute() -> Membership:
        user = User.objects.get(pk=user_id)
        membership = Membership(
            user=user,
            role=role,
            branch_id=branch_id,
            company_id=company_id,
            valid_from=valid_from or db_now(),
            valid_until=valid_until,
        )
        membership.full_clean()
        _check_membership_target(membership, user)
        membership.save()
        if membership.role in PRIVILEGED_ROLES:
            _elevate_privilege(user.pk)
        record(
            principal.audit_identity,
            "membership.granted",
            obj=membership,
            branch_id=membership.branch_id,
            data={
                **_snapshot(
                    membership,
                    ("user_id", "role", "branch_id", "company_id", "valid_from", "valid_until"),
                ),
                "granted_by": actor.pk,
            },
        )
        events.publish("membership.granted", membership_id=membership.pk, user_id=user.pk)
        return membership

    return execute_once(
        actor_id=principal.user_id,
        key=idempotency_key,
        operation="membership.grant",
        payload=payload,
        execute=execute,
        replay=lambda _type, pk: Membership.objects.get(pk=pk),
    )


def _open_membership(membership_id: UUID) -> Membership:
    membership = Membership.objects.get(pk=membership_id)
    if membership.revoked_at is not None:
        raise ValidationError(_("This membership is already revoked."), code="revoked")
    return membership


@transactional
def revoke_membership(principal: Principal, membership_id: UUID) -> Membership:
    """Revoke a membership now (database clock); it is never deleted."""
    # Discover the user to lock; a membership's user never changes, so no re-check.
    user_id = Membership.objects.values_list("user_id", flat=True).get(pk=membership_id)
    actor = lock_for_user_change(principal, user_id, Capability.ORG_MANAGE)
    membership = _open_membership(membership_id)
    membership.revoked_at = db_now()
    membership.revoked_by = actor
    membership.save(update_fields=["revoked_at", "revoked_by", "updated_at"])
    record(
        principal.audit_identity,
        "membership.revoked",
        obj=membership,
        branch_id=membership.branch_id,
        data={"user_id": user_id, "role": membership.role},
    )
    events.publish("membership.revoked", membership_id=membership.pk, user_id=user_id)
    return membership


@transactional
def change_membership_validity(
    principal: Principal,
    membership_id: UUID,
    *,
    valid_from: datetime,
    valid_until: datetime | None,
) -> Membership:
    """Move a membership's validity window; a privileged role elevates again."""
    user_id = Membership.objects.values_list("user_id", flat=True).get(pk=membership_id)
    lock_for_user_change(principal, user_id, Capability.ORG_MANAGE)
    membership = _open_membership(membership_id)
    diff = apply_changes(
        membership,
        {"valid_from": valid_from, "valid_until": valid_until},
        {"valid_from", "valid_until"},
    )
    if not diff:
        return membership
    membership.full_clean()
    membership.save(update_fields=["valid_from", "valid_until", "updated_at"])
    if membership.role in PRIVILEGED_ROLES:
        _elevate_privilege(user_id)
    record(
        principal.audit_identity,
        "membership.validity_changed",
        obj=membership,
        branch_id=membership.branch_id,
        data=diff,
    )
    events.publish("membership.validity_changed", membership_id=membership.pk, user_id=user_id)
    return membership


@transactional
def set_employer_company(principal: Principal, user_id: UUID, company_id: UUID | None) -> User:
    """Set the company that employs a user (plan §3.3).

    Refused while the user holds an open partner membership on another company: that
    membership is on the employer (plan §3.4).
    """
    lock_for_user_change(principal, user_id, Capability.ORG_MANAGE)
    user = User.objects.get(pk=user_id)
    diff = apply_changes(user, {"employer_company_id": company_id}, {"employer_company_id"})
    if not diff:
        return user
    if (
        company_id is not None
        and not Company.objects.filter(pk=company_id, is_active=True).exists()
    ):
        raise ValidationError(
            {"employer_company": ValidationError(_("The company is inactive."), code="inactive")}
        )
    open_partner = Membership.objects.filter(
        user_id=user_id, role=Role.PARTNER_REQUESTER, revoked_at__isnull=True
    )
    if company_id is not None:
        open_partner = open_partner.exclude(company_id=company_id)
    if open_partner.exists():
        raise ValidationError(
            {
                "employer_company": ValidationError(
                    _("Revoke the partner memberships on the previous company first."),
                    code="partner_memberships",
                )
            }
        )
    user.save(update_fields=["employer_company"])
    record(principal.audit_identity, "user.employer_changed", obj=user, data=diff)
    events.publish("user.employer_changed", user_id=user.pk)
    return user

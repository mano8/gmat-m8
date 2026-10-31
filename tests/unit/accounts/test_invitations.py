"""Invitations: creation, resend, revoke and acceptance (plan §5, "Invitations")."""

from collections.abc import Callable
from datetime import timedelta

import pytest
from allauth.account.models import EmailAddress
from allauth.mfa.models import Authenticator
from django.core.exceptions import ValidationError
from django.utils import timezone

from apps.access.exceptions import AccessDenied, TokenRejected
from apps.access.principals import SessionPrincipal, TokenKind, TokenPrincipal
from apps.access.roles import Role
from apps.accounts import services, tokens
from apps.accounts.models import Invitation, User
from apps.accounts.services import AssignmentGrant, MembershipGrant, parse_grants
from apps.audit.models import AuditEvent
from apps.core.idempotency import issue_key
from apps.core.locking import LockSetChanged
from apps.notifications.models import EmailOutbox, Status
from apps.organizations.models import Branch, Company, Membership
from apps.projects.models import Project, ProjectAssignment
from apps.projects.services import create_project
from tests.support import PASSWORD, enroll_totp, error_codes, grant, principal_for
from tests.tokens import last_token, tokens_sent_to

pytestmark = pytest.mark.django_db

NEW_PASSWORD = "a-brand-new-password-9"


def accept(
    raw: str, *, session_user: User | None = None, password: str | None = NEW_PASSWORD
) -> User:
    principal = tokens.invitation_principal(
        tokens.hash_token(raw), session_user_id=session_user.pk if session_user else None
    )
    return services.accept_invitation(principal, password=password)


class TestTokens:
    def test_tokens_are_random_and_only_their_hash_is_stored(
        self, invite: Callable[..., Invitation]
    ) -> None:
        raw, digest = tokens.new_token()
        assert len(raw) == 43 and digest == tokens.hash_token(raw) and len(digest) == 64
        assert tokens.new_token()[0] != raw
        invitation = invite()
        sent = last_token("new@example.org")
        assert invitation.token_hash == tokens.hash_token(sent)
        assert sent not in str(Invitation.objects.filter(pk=invitation.pk).values().get())

    def test_same_hash_is_constant_time_equality(self) -> None:
        assert tokens.same_hash("ab", "ab") and not tokens.same_hash("ab", "ac")

    def test_unknown_tokens_are_rejected_at_once(self) -> None:
        with pytest.raises(TokenRejected):
            tokens.invitation_principal(tokens.hash_token("x" * 43))
        with pytest.raises(TokenRejected):
            tokens.email_change_principal(tokens.hash_token("x" * 43), "confirm")


class TestCreate:
    def test_new_account_invitation(
        self, invite: Callable[..., Invitation], admin_principal: SessionPrincipal, branch: Branch
    ) -> None:
        key = issue_key(admin_principal.user_id, "invitation.create")
        invitation = invite("New@Example.org", full_name="Nora New", idempotency_key=key)
        assert (invitation.email, invitation.kind, invitation.target_user) == (
            "new@example.org",
            Invitation.Kind.NEW_ACCOUNT,
            None,
        )
        assert invitation.invited_by_id == admin_principal.user_id
        assert invitation.expires_at - invitation.created_at == timedelta(hours=72)
        memberships, assignments = parse_grants(invitation.grants)
        assert memberships == [MembershipGrant(role=Role.TECHNICIAN, branch_id=branch.pk)]
        assert assignments == []
        mail = EmailOutbox.objects.get()
        assert (mail.recipient_address, mail.sensitive, mail.eligibility_rule) == (
            "new@example.org",
            True,
            services.INVITATION_PENDING,
        )
        assert AuditEvent.objects.filter(action="invitation.created", object_id=invitation.pk)
        # The same form submitted twice creates one invitation.
        again = invite("new@example.org", full_name="Nora New", idempotency_key=key)
        assert again.pk == invitation.pk and Invitation.objects.count() == 1

    def test_existing_accounts_get_an_existing_account_invitation(
        self, invite: Callable[..., Invitation], make_user: Callable[..., User]
    ) -> None:
        user = make_user("old@example.org")
        invitation = invite("old@example.org")
        assert (invitation.kind, invitation.target_user) == (Invitation.Kind.EXISTING_ACCOUNT, user)

    def test_one_pending_invitation_per_address(self, invite: Callable[..., Invitation]) -> None:
        invite()
        with pytest.raises(ValidationError) as exc:
            invite()
        assert error_codes(exc) == {"pending"}

    def test_grants_are_validated(
        self, invite: Callable[..., Invitation], admin_principal: SessionPrincipal, branch: Branch
    ) -> None:
        with pytest.raises(ValidationError) as exc:
            services.create_invitation(admin_principal, email="x@example.org", memberships=[])
        assert error_codes(exc) == {"no_grant"}
        with pytest.raises(ValidationError):
            invite(role=Role.TECHNICIAN, branch_id=None)  # a branch role without its branch
        Branch.objects.filter(pk=branch.pk).update(is_active=False)
        with pytest.raises(ValidationError) as exc:
            invite()
        assert error_codes(exc) == {"inactive"}

    def test_a_partner_is_on_their_employer(
        self, invite: Callable[..., Invitation], admin_principal: SessionPrincipal, company: Company
    ) -> None:
        other = Company.objects.create(code="C", legal_name="Company C")
        with pytest.raises(ValidationError) as exc:
            services.create_invitation(
                admin_principal,
                email="p@example.org",
                employer_company_id=other.pk,
                memberships=[MembershipGrant(role=Role.PARTNER_REQUESTER, company_id=company.pk)],
            )
        assert error_codes(exc) == {"not_employer"}
        invitation = invite("p@example.org", Role.PARTNER_REQUESTER, employer_company_id=other.pk)
        assert invitation.employer_company_id == other.pk

    def test_an_active_account_keeps_its_employer(
        self, invite: Callable[..., Invitation], make_user: Callable[..., User], company: Company
    ) -> None:
        make_user("old@example.org")
        with pytest.raises(ValidationError) as exc:
            invite("old@example.org", employer_company_id=company.pk)
        assert error_codes(exc) == {"employer"}

    def test_assignments_need_an_active_project(
        self, invite: Callable[..., Invitation], admin_principal: SessionPrincipal, branch: Branch
    ) -> None:
        project = create_project(admin_principal, code="P-1", name="P", branch_id=branch.pk)
        grant_ = AssignmentGrant(project_id=project.pk, kind=ProjectAssignment.Kind.MANAGER)
        invitation = invite(role=Role.SITE_MANAGER, assignments=(grant_,))
        assert parse_grants(invitation.grants)[1] == [grant_]
        Project.objects.filter(pk=project.pk).update(status=Project.Status.CLOSED)
        with pytest.raises(ValidationError) as exc:
            invite("b@example.org", role=Role.SITE_MANAGER, assignments=(grant_,))
        assert error_codes(exc) == {"inactive"}
        with pytest.raises(ValidationError) as exc:
            invite(
                "c@example.org",
                role=Role.SITE_MANAGER,
                assignments=(AssignmentGrant(project_id=project.pk, kind="BOSS"),),
            )
        assert error_codes(exc) == {"invalid"}

    def test_only_administrators_invite(
        self, invite: Callable[..., Invitation], member: Callable[..., User], branch: Branch
    ) -> None:
        manager = member(Role.MATERIAL_MANAGER, branch=branch)
        with pytest.raises(AccessDenied):
            invite(principal=principal_for(manager))

    def test_a_group_admin_cannot_invite_a_staff_account(
        self, invite: Callable[..., Invitation], make_user: Callable[..., User]
    ) -> None:
        make_user("op@example.org", is_staff=True)
        with pytest.raises(AccessDenied, match="platform operator"):
            invite("op@example.org")
        assert not Invitation.objects.exists()

    def test_a_superuser_administrator_can(
        self, invite: Callable[..., Invitation], make_user: Callable[..., User]
    ) -> None:
        operator = make_user("root@example.org", is_staff=True, is_superuser=True)
        grant(operator, Role.GROUP_ADMIN)
        make_user("op@example.org", is_staff=True)
        assert invite("op@example.org", principal=principal_for(operator)).kind == (
            Invitation.Kind.EXISTING_ACCOUNT
        )


class TestResendAndRevoke:
    def test_resend_kills_the_old_token(
        self, invite: Callable[..., Invitation], admin_principal: SessionPrincipal
    ) -> None:
        old = invite()
        first = last_token("new@example.org")
        new = services.resend_invitation(admin_principal, old.pk)
        old.refresh_from_db()
        assert (old.revoked_at is not None, old.revoke_reason) == (True, services.REASON_RESENT)
        assert new.pk != old.pk and new.grants == old.grants and new.is_pending
        second = last_token("new@example.org")
        assert first != second
        assert EmailOutbox.objects.get(eligibility_object_id=old.pk).status == Status.EXPIRED
        with pytest.raises(TokenRejected):
            accept(first)
        assert accept(second).email == "new@example.org"

    def test_revoke_kills_the_token(
        self, invite: Callable[..., Invitation], admin_principal: SessionPrincipal
    ) -> None:
        invitation = invite()
        raw = last_token("new@example.org")
        services.revoke_invitation(admin_principal, invitation.pk, reason="  wrong person ")
        invitation.refresh_from_db()
        assert invitation.revoke_reason == "wrong person"
        assert tokens_sent_to("new@example.org") == []  # the unsent email was wiped
        with pytest.raises(TokenRejected):
            accept(raw)
        with pytest.raises(ValidationError) as exc:
            services.resend_invitation(admin_principal, invitation.pk)
        assert error_codes(exc) == {"not_pending"}
        assert AuditEvent.objects.filter(action="invitation.revoked")


class TestAcceptNewAccount:
    def test_creates_the_account_verified_with_its_grants(
        self, invite: Callable[..., Invitation], branch: Branch, company: Company
    ) -> None:
        invitation = invite(full_name="Nora New", employer_company_id=company.pk)
        user = accept(last_token("new@example.org"))
        assert user.check_password(NEW_PASSWORD) and user.is_active
        assert (user.full_name, user.employer_company_id) == ("Nora New", company.pk)
        assert not user.is_staff and not user.is_superuser
        assert EmailAddress.objects.filter(user=user, email=user.email, verified=True, primary=True)
        membership = Membership.objects.get(user=user)
        assert (membership.role, membership.branch_id) == (Role.TECHNICIAN, branch.pk)
        invitation.refresh_from_db()
        assert invitation.accepted_at is not None and invitation.target_user == user
        event = AuditEvent.objects.get(action="invitation.accepted")
        assert (event.actor_id, event.via) == (user.pk, f"invitation:{invitation.pk}")

    def test_single_use(self, invite: Callable[..., Invitation]) -> None:
        invite()
        raw = last_token("new@example.org")
        accept(raw)
        with pytest.raises(TokenRejected):
            accept(raw, password="another-password-12")
        assert User.objects.filter(email="new@example.org").count() == 1

    def test_expired(self, invite: Callable[..., Invitation]) -> None:
        invitation = invite()
        Invitation.objects.filter(pk=invitation.pk).update(
            expires_at=timezone.now() - timedelta(seconds=1)
        )
        with pytest.raises(TokenRejected, match="expired"):
            accept(last_token("new@example.org"))

    def test_signed_out_only(
        self, invite: Callable[..., Invitation], make_user: Callable[..., User]
    ) -> None:
        invite()
        with pytest.raises(TokenRejected, match=tokens.SIGN_OUT_REQUIRED):
            accept(last_token("new@example.org"), session_user=make_user("someone@example.org"))

    def test_an_account_created_meanwhile_wins(
        self, invite: Callable[..., Invitation], make_user: Callable[..., User]
    ) -> None:
        invite()
        make_user("new@example.org")
        with pytest.raises(TokenRejected):
            accept(last_token("new@example.org"))

    def test_needs_a_password(self, invite: Callable[..., Invitation]) -> None:
        invite()
        with pytest.raises(ValueError, match="password"):
            accept(last_token("new@example.org"), password=None)

    def test_a_privileged_role_elevates(self, invite: Callable[..., Invitation]) -> None:
        invite(role=Role.MATERIAL_MANAGER)
        user = accept(last_token("new@example.org"))
        user.refresh_from_db()
        assert user.privilege_elevated_at is not None

    def test_inactive_grants_refuse_the_acceptance(
        self, invite: Callable[..., Invitation], branch: Branch
    ) -> None:
        invite()
        Branch.objects.filter(pk=branch.pk).update(is_active=False)
        with pytest.raises(TokenRejected, match="no longer active"):
            accept(last_token("new@example.org"))
        assert not User.objects.filter(email="new@example.org").exists()

    def test_the_inviter_must_still_hold_org_manage(
        self, invite: Callable[..., Invitation], group_admin: User
    ) -> None:
        invite()
        Membership.objects.filter(user=group_admin).update(
            valid_until=timezone.now() - timedelta(seconds=1),
            valid_from=timezone.now() - timedelta(hours=2),
        )
        with pytest.raises(TokenRejected, match="issuer"):
            accept(last_token("new@example.org"))

    def test_assignments_are_created_once(
        self, invite: Callable[..., Invitation], admin_principal: SessionPrincipal, branch: Branch
    ) -> None:
        project = create_project(admin_principal, code="P-1", name="P", branch_id=branch.pk)
        grant_ = AssignmentGrant(project_id=project.pk, kind=ProjectAssignment.Kind.MANAGER)
        invite(role=Role.SITE_MANAGER, assignments=(grant_, grant_))
        user = accept(last_token("new@example.org"))
        assert ProjectAssignment.objects.filter(user=user, project=project).count() == 1


class TestAcceptExistingAccount:
    def test_only_that_account_signed_in_accepts(
        self, invite: Callable[..., Invitation], make_user: Callable[..., User], branch: Branch
    ) -> None:
        user = make_user("old@example.org")
        invite("old@example.org", role=Role.WAREHOUSE_OPERATOR)
        raw = last_token("old@example.org")
        with pytest.raises(TokenRejected, match=tokens.SIGN_IN_REQUIRED):
            accept(raw, password=None)
        with pytest.raises(TokenRejected, match=tokens.SIGN_OUT_REQUIRED):
            accept(raw, session_user=make_user("other@example.org"), password=None)
        password_hash, epoch = user.password, user.session_epoch
        assert accept(raw, session_user=user, password=None) == user
        user.refresh_from_db()
        assert (user.password, user.session_epoch) == (password_hash, epoch)
        assert Membership.objects.get(user=user).role == Role.WAREHOUSE_OPERATOR

    def test_never_changes_an_active_accounts_password(
        self, invite: Callable[..., Invitation], make_user: Callable[..., User]
    ) -> None:
        user = make_user("old@example.org")
        invite("old@example.org")
        with pytest.raises(ValueError, match="never changes"):
            accept(last_token("old@example.org"), session_user=user, password="hijack-password-1")
        user.refresh_from_db()
        assert user.check_password(PASSWORD)

    def test_rehire_after_offboarding(
        self,
        invite: Callable[..., Invitation],
        admin_principal: SessionPrincipal,
        member: Callable[..., User],
        branch: Branch,
    ) -> None:
        user = member(Role.TECHNICIAN, email="old@example.org", branch=branch)
        enroll_totp(user)
        services.offboard_user(admin_principal, user.pk)
        invite("old@example.org")
        rehired = accept(last_token("old@example.org"))
        assert rehired.pk == user.pk and rehired.is_active and rehired.offboarded_at is None
        assert rehired.check_password(NEW_PASSWORD)
        assert not Authenticator.objects.filter(user=user).exists()
        assert Membership.objects.filter(user=user, revoked_at__isnull=True).count() == 1
        assert AuditEvent.objects.get(action="invitation.accepted").data["reactivated"] is True

    def test_an_invitation_older_than_the_offboarding_cannot_reactivate(
        self, invite: Callable[..., Invitation], member: Callable[..., User], branch: Branch
    ) -> None:
        user = member(Role.TECHNICIAN, email="old@example.org", branch=branch)
        invitation = invite("old@example.org")
        # The offboarding's own revocation aside: an offboarding after the invitation.
        User.objects.filter(pk=user.pk).update(
            is_active=False, offboarded_at=invitation.created_at + timedelta(seconds=1)
        )
        with pytest.raises(TokenRejected, match="before the account was offboarded"):
            accept(last_token("old@example.org"))
        user.refresh_from_db()
        assert not user.is_active

    def test_a_returning_account_is_reactivated_signed_out(
        self, invite: Callable[..., Invitation], make_user: Callable[..., User]
    ) -> None:
        make_user("old@example.org", is_active=False)
        invite("old@example.org")
        with pytest.raises(TokenRejected, match=tokens.SIGN_OUT_REQUIRED):
            accept(last_token("old@example.org"), session_user=make_user("x@example.org"))
        with pytest.raises(ValueError, match="returning"):
            accept(last_token("old@example.org"), password=None)

    def test_a_staff_target_needs_a_superuser_issuer_at_acceptance(
        self, invite: Callable[..., Invitation], make_user: Callable[..., User]
    ) -> None:
        operator = make_user("root@example.org", is_staff=True, is_superuser=True)
        grant(operator, Role.GROUP_ADMIN)
        target = make_user("op@example.org", is_staff=True, is_active=False)
        invite("op@example.org", principal=principal_for(operator))
        User.objects.filter(pk=operator.pk).update(is_superuser=False)
        with pytest.raises(AccessDenied):
            accept(last_token("op@example.org"))
        target.refresh_from_db()
        assert not target.is_active


def test_mail_only_while_the_invitation_is_pending(invite: Callable[..., Invitation]) -> None:
    from apps.accounts.eligibility import invitation_pending

    invitation = invite()
    mail = EmailOutbox.objects.get()
    assert invitation_pending(mail)
    Invitation.objects.filter(pk=invitation.pk).update(
        expires_at=timezone.now() - timedelta(hours=1)
    )
    assert not invitation_pending(mail)
    mail.eligibility_object_type = "accounts.user"
    assert not invitation_pending(mail)
    assert tokens_sent_to("nobody@example.org") == []


class TestEdges:
    def test_a_principal_without_a_token_row_is_refused(self) -> None:
        principal = TokenPrincipal(
            kind=TokenKind.INVITATION,
            operation=tokens.ACCEPT_INVITATION,
            target_user_id=None,
            target_email="x@example.org",
            check_token=lambda target, locks: None,
        )
        with pytest.raises(TokenRejected, match="no token row"):
            services.accept_invitation(principal, password=NEW_PASSWORD)

    def test_an_account_appearing_after_discovery_changes_the_lock_set(
        self, invite: Callable[..., Invitation], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The shared wrapper retries ``LockSetChanged``; inside the test's own transaction
        there is nothing to retry, so it reaches the test."""
        original = services._lock_and_authorize

        def racing(principal: SessionPrincipal, target_id: object) -> User:
            User.objects.create_user("new@example.org")  # created after the discovery
            return original(principal, target_id)  # type: ignore[arg-type]

        monkeypatch.setattr(services, "_lock_and_authorize", racing)
        with pytest.raises(LockSetChanged):
            invite()
        assert not Invitation.objects.exists()

    def test_a_bootstrap_link_is_not_resent(self, admin_principal: SessionPrincipal) -> None:
        bootstrap = Invitation.objects.create(
            email="boss@example.org",
            kind=Invitation.Kind.NEW_ACCOUNT,
            bootstrap=True,
            token_hash=tokens.new_token()[1],
            expires_at=timezone.now() + timedelta(hours=1),
        )
        with pytest.raises(ValidationError) as exc:
            services.resend_invitation(admin_principal, bootstrap.pk)
        assert error_codes(exc) == {"bootstrap"}

    def test_a_rehire_takes_the_new_employer_and_name(
        self,
        invite: Callable[..., Invitation],
        admin_principal: SessionPrincipal,
        member: Callable[..., User],
        branch: Branch,
    ) -> None:
        user = member(Role.TECHNICIAN, email="old@example.org", branch=branch)
        services.offboard_user(admin_principal, user.pk)
        other = Company.objects.create(code="C", legal_name="Company C")
        invite(
            "old@example.org",
            Role.PARTNER_REQUESTER,
            employer_company_id=other.pk,
            full_name="Olga Old",
        )
        rehired = accept(last_token("old@example.org"))
        assert (rehired.employer_company_id, rehired.full_name) == (other.pk, "Olga Old")

    def test_a_vanished_target_is_a_problem(self, invite: Callable[..., Invitation]) -> None:
        invitation = invite()
        invitation.kind = Invitation.Kind.EXISTING_ACCOUNT
        assert tokens.invitation_problem(invitation, None, None) == "the account no longer exists"

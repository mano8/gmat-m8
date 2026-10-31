"""Accounts: the custom user (email login, UUIDv7 key, the counters access control relies on),
invitations and administrator-initiated email changes (plan §3.3, §5)."""

from typing import Any

from django.contrib.auth.models import AbstractBaseUser, BaseUserManager, PermissionsMixin
from django.db import models
from django.db.models.functions import Lower
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from apps.core.ids import new_id
from apps.core.models import BaseModel


class SuperuserCreationDisabled(RuntimeError):
    """Accounts never skip the invitation path (plan §5, "First administrator")."""


class UserManager(BaseUserManager["User"]):
    use_in_migrations = True

    def _create(self, email: str, password: str | None, **extra: Any) -> User:
        if not email:
            raise ValueError("An email address is required")
        user = self.model(email=self.normalize_email(email).lower(), **extra)
        user.set_password(password)
        user.save(using=self._db)
        return user

    def create_user(self, email: str, password: str | None = None, **extra: Any) -> User:
        extra.setdefault("is_staff", False)
        extra.setdefault("is_superuser", False)
        return self._create(email, password, **extra)

    def create_superuser(self, email: str, password: str | None = None, **extra: Any) -> User:
        """Disabled: Django's ``createsuperuser`` ends here (F76).

        The first administrator, a platform operator included, arrives through
        ``manage bootstrap_admin`` and its invitation, which sets a password and MFA.
        """
        raise SuperuserCreationDisabled(
            "createsuperuser is disabled: run `manage bootstrap_admin --superuser` instead"
        )


class User(AbstractBaseUser, PermissionsMixin):
    """An account. ``is_superuser`` is for platform operators, never for business actions."""

    id = models.UUIDField(primary_key=True, default=new_id, editable=False)
    # Stored lowercase (CHECK below), so plain uniqueness is case-insensitive.
    email = models.EmailField(max_length=254, unique=True)
    full_name = models.CharField(max_length=150, blank=True)
    is_active = models.BooleanField(default=True)
    is_staff = models.BooleanField(default=False)
    date_joined = models.DateTimeField(default=timezone.now)
    offboarded_at = models.DateTimeField(null=True, blank=True)
    # Bumped whenever the user's authority changes; checked inside every write transaction.
    authz_generation = models.PositiveBigIntegerField(default=0)
    # Bumped on password, email or MFA change and on deactivation; older sessions stop working.
    session_epoch = models.PositiveBigIntegerField(default=0)
    privilege_elevated_at = models.DateTimeField(null=True, blank=True)
    # The company that employs the user; a partner's memberships are on this company (§3.4).
    employer_company = models.ForeignKey(
        "organizations.Company",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="employees",
    )

    objects = UserManager()

    USERNAME_FIELD = "email"
    EMAIL_FIELD = "email"
    REQUIRED_FIELDS = []  # email is the login; nothing else is prompted

    class Meta:
        constraints = [
            models.CheckConstraint(
                condition=models.Q(email=Lower("email")), name="accounts_user_email_lowercase"
            ),
        ]

    def __str__(self) -> str:
        return self.email

    def clean(self) -> None:
        super().clean()
        self.email = self.email.strip().lower()


class Invitation(BaseModel):
    """How an account arrives or comes back (plan §3.3, §5 "Invitations").

    Only the SHA-256 of the token is stored (``apps.accounts.tokens``). ``grants`` lists the
    memberships and project assignments that acceptance creates. A bootstrap invitation
    (``manage bootstrap_admin``) has no ``invited_by``; it alone may make the new account a
    platform operator. ``created_at`` is the database clock after the target row was locked,
    so "created after ``offboarded_at``" orders correctly against an offboarding.
    """

    class Kind(models.TextChoices):
        NEW_ACCOUNT = "NEW_ACCOUNT", _("New account")
        EXISTING_ACCOUNT = "EXISTING_ACCOUNT", _("Existing account")

    email = models.EmailField(_("email"), max_length=254)
    kind = models.CharField(_("kind"), max_length=20, choices=Kind)
    # Set at creation for an existing account; for a new one, once acceptance created it.
    target_user = models.ForeignKey(
        User,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="invitations",
        verbose_name=_("account"),
    )
    invited_by = models.ForeignKey(
        User,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="+",
        verbose_name=_("invited by"),
    )
    bootstrap = models.BooleanField(_("first administrator"), default=False)
    platform_operator = models.BooleanField(_("platform operator"), default=False)
    full_name = models.CharField(_("full name"), max_length=150, blank=True)
    employer_company = models.ForeignKey(
        "organizations.Company",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="+",
        verbose_name=_("employer"),
    )
    grants = models.JSONField(_("grants"), default=dict, blank=True)
    token_hash = models.CharField(max_length=64, unique=True, editable=False)
    expires_at = models.DateTimeField(_("expires at"))
    accepted_at = models.DateTimeField(_("accepted at"), null=True, blank=True)
    revoked_at = models.DateTimeField(_("revoked at"), null=True, blank=True)
    revoke_reason = models.CharField(_("reason"), max_length=200, blank=True)

    class Meta:
        verbose_name = _("invitation")
        verbose_name_plural = _("invitations")
        ordering = ["-created_at"]
        constraints = [
            models.CheckConstraint(
                condition=models.Q(email=Lower("email")),
                name="accounts_invitation_email_lowercase",
            ),
            models.CheckConstraint(
                condition=models.Q(kind__in=["NEW_ACCOUNT", "EXISTING_ACCOUNT"]),
                name="accounts_invitation_kind_valid",
            ),
            models.CheckConstraint(
                condition=~models.Q(kind="EXISTING_ACCOUNT") | models.Q(target_user__isnull=False),
                name="accounts_invitation_existing_has_target",
            ),
            models.CheckConstraint(
                condition=models.Q(bootstrap=True, invited_by__isnull=True)
                | models.Q(bootstrap=False, invited_by__isnull=False),
                name="accounts_invitation_bootstrap_iff_no_inviter",
            ),
            models.CheckConstraint(
                condition=models.Q(platform_operator=False) | models.Q(bootstrap=True),
                name="accounts_invitation_operator_only_bootstrap",
            ),
            models.CheckConstraint(
                condition=models.Q(accepted_at__isnull=True) | models.Q(revoked_at__isnull=True),
                name="accounts_invitation_accepted_or_revoked",
            ),
        ]
        indexes = [
            models.Index(
                fields=["email"],
                condition=models.Q(accepted_at__isnull=True, revoked_at__isnull=True),
                name="accounts_invitation_pending",
            ),
            models.Index(fields=["invited_by"], name="accounts_invitation_inviter"),
        ]

    def __str__(self) -> str:
        return f"{self.email} ({self.kind})"

    @property
    def is_pending(self) -> bool:
        return self.accepted_at is None and self.revoked_at is None


class EmailChangeRequest(BaseModel):
    """An administrator-initiated change of a user's email (plan §3.3, §5, Q26, Q28).

    The new address confirms with ``token_hash`` before ``expires_at``; the old address can
    cancel with ``cancel_token_hash`` until the change is applied. Confirmation starts the
    cooling-off: the worker applies the change once ``apply_after`` has passed. The
    initiator's authority is re-checked at confirmation and at application.
    """

    user = models.ForeignKey(
        User,
        on_delete=models.PROTECT,
        related_name="email_change_requests",
        verbose_name=_("account"),
    )
    new_email = models.EmailField(_("new email"), max_length=254)
    requested_by = models.ForeignKey(
        User, on_delete=models.PROTECT, related_name="+", verbose_name=_("requested by")
    )
    token_hash = models.CharField(max_length=64, unique=True, editable=False)
    cancel_token_hash = models.CharField(max_length=64, unique=True, editable=False)
    expires_at = models.DateTimeField(_("confirm before"))
    confirmed_at = models.DateTimeField(_("confirmed at"), null=True, blank=True)
    apply_after = models.DateTimeField(_("applies after"), null=True, blank=True)
    applied_at = models.DateTimeField(_("applied at"), null=True, blank=True)
    cancelled_at = models.DateTimeField(_("cancelled at"), null=True, blank=True)
    revoked_at = models.DateTimeField(_("revoked at"), null=True, blank=True)
    revoke_reason = models.CharField(_("reason"), max_length=200, blank=True)

    class Meta:
        verbose_name = _("email change request")
        verbose_name_plural = _("email change requests")
        ordering = ["-created_at"]
        constraints = [
            models.CheckConstraint(
                condition=models.Q(new_email=Lower("new_email")),
                name="accounts_emailchange_email_lowercase",
            ),
            models.CheckConstraint(
                condition=models.Q(confirmed_at__isnull=True, apply_after__isnull=True)
                | models.Q(confirmed_at__isnull=False, apply_after__isnull=False),
                name="accounts_emailchange_apply_after_confirmed",
            ),
            models.CheckConstraint(
                condition=models.Q(applied_at__isnull=True) | models.Q(confirmed_at__isnull=False),
                name="accounts_emailchange_applied_confirmed",
            ),
            # Applied, cancelled and revoked exclude each other.
            models.CheckConstraint(
                condition=models.Q(cancelled_at__isnull=True, revoked_at__isnull=True)
                | models.Q(applied_at__isnull=True, cancelled_at__isnull=True)
                | models.Q(applied_at__isnull=True, revoked_at__isnull=True),
                name="accounts_emailchange_one_ending",
            ),
            models.UniqueConstraint(
                fields=["user"],
                condition=models.Q(
                    applied_at__isnull=True, cancelled_at__isnull=True, revoked_at__isnull=True
                ),
                name="accounts_emailchange_one_open",
            ),
        ]
        indexes = [models.Index(fields=["requested_by"], name="accounts_emailchange_initiator")]

    def __str__(self) -> str:
        return f"{self.user_id} → {self.new_email}"

    @property
    def is_open(self) -> bool:
        return self.applied_at is None and self.cancelled_at is None and self.revoked_at is None

"""Custom user: email login, UUIDv7 key, and the generation counters access control relies on."""

from typing import Any

from django.contrib.auth.models import AbstractBaseUser, BaseUserManager, PermissionsMixin
from django.db import models
from django.db.models.functions import Lower
from django.utils import timezone

from apps.core.ids import new_id


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
        extra["is_staff"] = True
        extra["is_superuser"] = True
        return self._create(email, password, **extra)


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

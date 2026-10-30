"""Companies, joint ventures, branches and memberships (plan §3.3).

Models hold data and constraints only; every change goes through ``services.py``. Codes are
fixed at creation: document numbers and sequence scopes are built from them (plan §4.2).
"""

from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.validators import RegexValidator
from django.db import models
from django.db.models import Q
from django.utils.translation import gettext_lazy as _

from apps.access.roles import BRANCH_ROLES, INSTALLATION_ROLES, Role
from apps.core.models import BaseModel

CODE_PATTERN = r"^[A-Z0-9][A-Z0-9_-]*$"
code_validator = RegexValidator(
    CODE_PATTERN, _("Use upper-case letters, digits, '-' and '_', starting with a letter or digit.")
)


class Company(BaseModel):
    class Kind(models.TextChoices):
        STANDARD = "STANDARD", _("Standard")
        JOINT_VENTURE = "JOINT_VENTURE", _("Joint venture")

    code = models.CharField(_("code"), max_length=20, unique=True, validators=[code_validator])
    legal_name = models.CharField(_("legal name"), max_length=200)
    tax_id = models.CharField(_("tax id"), max_length=32, unique=True, null=True, blank=True)
    kind = models.CharField(_("kind"), max_length=20, choices=Kind, default=Kind.STANDARD)
    is_active = models.BooleanField(_("active"), default=True)

    class Meta:
        verbose_name = _("company")
        verbose_name_plural = _("companies")
        ordering = ["code"]
        constraints = [
            models.CheckConstraint(
                condition=Q(code__regex=CODE_PATTERN), name="organizations_company_code_format"
            ),
            models.CheckConstraint(
                condition=Q(kind__in=["STANDARD", "JOINT_VENTURE"]),
                name="organizations_company_kind_valid",
            ),
            models.CheckConstraint(
                condition=~Q(tax_id=""), name="organizations_company_tax_id_not_empty"
            ),
            # Target of the joint-venture member's (venture_id, venture_kind) foreign key.
            models.UniqueConstraint(fields=["id", "kind"], name="organizations_company_id_kind"),
        ]

    def __str__(self) -> str:
        return f"{self.code} · {self.legal_name}"

    def clean(self) -> None:
        super().clean()
        self.tax_id = (self.tax_id or "").strip() or None


class JointVentureMember(BaseModel):
    """A member company of a joint venture; informational in the MVP (plan §3.3).

    ``venture_kind`` is a fixed copy that lets a composite foreign key hold
    ``venture.kind = JOINT_VENTURE``: a venture with members cannot become ``STANDARD``.
    """

    venture = models.ForeignKey(
        Company, on_delete=models.PROTECT, related_name="venture_members", verbose_name=_("venture")
    )
    venture_kind = models.CharField(
        max_length=20, default=Company.Kind.JOINT_VENTURE, editable=False
    )
    member = models.ForeignKey(
        Company, on_delete=models.PROTECT, related_name="ventures", verbose_name=_("member")
    )
    share_pct = models.DecimalField(
        _("share (%)"), max_digits=5, decimal_places=2, null=True, blank=True
    )

    class Meta:
        verbose_name = _("joint-venture member")
        verbose_name_plural = _("joint-venture members")
        constraints = [
            models.UniqueConstraint(fields=["venture", "member"], name="organizations_jv_pair"),
            models.CheckConstraint(
                condition=~Q(venture=models.F("member")), name="organizations_jv_not_self"
            ),
            models.CheckConstraint(
                condition=Q(venture_kind="JOINT_VENTURE"), name="organizations_jv_venture_kind"
            ),
            models.CheckConstraint(
                condition=Q(share_pct__isnull=True) | Q(share_pct__gt=0, share_pct__lte=100),
                name="organizations_jv_share_range",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.venture_id} ← {self.member_id}"


class Branch(BaseModel):
    """A separately managed unit of a company (plan §3.3).

    ``managed_by`` is one level only: a managed branch manages nothing, and a managing
    branch is not managed. A trigger (``organizations.0001``) holds that rule under the
    ``AccessState`` lock; ``clean`` reports it to forms first.
    """

    company = models.ForeignKey(
        Company, on_delete=models.PROTECT, related_name="branches", verbose_name=_("company")
    )
    code = models.CharField(_("code"), max_length=20, validators=[code_validator])
    name = models.CharField(_("name"), max_length=200)
    city = models.CharField(_("city"), max_length=100, blank=True)
    address = models.TextField(_("address"), blank=True)
    managed_by = models.ForeignKey(
        "self",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="managed_branches",
        verbose_name=_("managed by"),
    )
    allow_self_approval = models.BooleanField(_("allow self-approval"), default=False)
    default_warehouse = models.ForeignKey(
        "locations.Location",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="+",
        verbose_name=_("default warehouse"),
    )
    is_active = models.BooleanField(_("active"), default=True)

    class Meta:
        verbose_name = _("branch")
        verbose_name_plural = _("branches")
        ordering = ["company__code", "code"]
        constraints = [
            models.UniqueConstraint(fields=["company", "code"], name="organizations_branch_code"),
            models.CheckConstraint(
                condition=Q(code__regex=CODE_PATTERN), name="organizations_branch_code_format"
            ),
            models.CheckConstraint(
                condition=~Q(managed_by=models.F("id")), name="organizations_branch_not_self"
            ),
        ]

    def __str__(self) -> str:
        return f"{self.company.code}-{self.code} · {self.name}"

    def clean(self) -> None:
        super().clean()
        if self.managed_by_id is None:
            return
        if self.managed_by_id == self.pk:
            raise ValidationError(
                {
                    "managed_by": ValidationError(
                        _("A branch cannot manage itself."), code="managed_by_self"
                    )
                }
            )
        if Branch.objects.filter(pk=self.managed_by_id, managed_by__isnull=False).exists():
            raise ValidationError(
                {
                    "managed_by": ValidationError(
                        _("A managed branch cannot manage other branches."),
                        code="managed_by_two_levels",
                    )
                }
            )
        if Branch.objects.filter(managed_by_id=self.pk).exists():
            raise ValidationError(
                {
                    "managed_by": ValidationError(
                        _("A branch that manages others cannot be managed."),
                        code="managed_by_two_levels",
                    )
                }
            )


def _role_shape(role: Role) -> Q:
    if role in INSTALLATION_ROLES:
        return Q(branch__isnull=True, company__isnull=True)
    if role in BRANCH_ROLES:
        return Q(branch__isnull=False, company__isnull=True)
    return Q(branch__isnull=True, company__isnull=False)  # COMPANY_ROLES


# In ``Role`` order, so the generated constraints are stable.
ROLE_SHAPES: dict[Role, Q] = {role: _role_shape(role) for role in Role}


class Membership(BaseModel):
    """A role held by a user, on a branch, a company or the installation (plan §3.4).

    Valid while not revoked and ``valid_from ≤ now < valid_until`` (S8). Revoked, never
    deleted: a trigger refuses ``DELETE``, and every change bumps the user's
    ``authz_generation`` (``access.0002``).
    """

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="memberships",
        verbose_name=_("user"),
    )
    role = models.CharField(_("role"), max_length=30, choices=Role)
    branch = models.ForeignKey(
        Branch,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="memberships",
        verbose_name=_("branch"),
    )
    company = models.ForeignKey(
        Company,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="memberships",
        verbose_name=_("company"),
    )
    valid_from = models.DateTimeField(_("valid from"))
    valid_until = models.DateTimeField(_("valid until"), null=True, blank=True)
    revoked_at = models.DateTimeField(_("revoked at"), null=True, blank=True)
    revoked_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="+",
        verbose_name=_("revoked by"),
    )

    class Meta:
        verbose_name = _("membership")
        verbose_name_plural = _("memberships")
        constraints = [
            models.CheckConstraint(
                condition=Q(role__in=[role.value for role in Role]),
                name="organizations_membership_role_valid",
            ),
            *(
                models.CheckConstraint(
                    condition=~Q(role=role.value) | shape,
                    name=f"organizations_membership_shape_{role.value.lower()}",
                )
                for role, shape in ROLE_SHAPES.items()
            ),
            models.CheckConstraint(
                condition=Q(valid_until__isnull=True) | Q(valid_until__gt=models.F("valid_from")),
                name="organizations_membership_validity",
            ),
            models.CheckConstraint(
                condition=Q(revoked_by__isnull=True) | Q(revoked_at__isnull=False),
                name="organizations_membership_revoked_by_needs_time",
            ),
        ]
        indexes = [models.Index(fields=["user", "role"], name="organizations_membership_user")]

    def __str__(self) -> str:
        return f"{self.user_id} · {self.role}"

"""Projects (worksites) and the assignments that share them (plan §3.3)."""

from django.conf import settings
from django.db import models
from django.db.models import Q
from django.utils.translation import gettext_lazy as _

from apps.access.scopes import Scope, ScopedQuerySet
from apps.core.models import BaseModel
from apps.organizations.models import CODE_PATTERN, Branch, code_validator


class ProjectQuerySet(ScopedQuerySet["Project"]):
    def scope_q(self, scope: Scope) -> Q:
        """Projects of the grants' branches (S3) or assigned to the user (S4)."""
        return scope.project_q()


class Project(BaseModel):
    """A worksite. ``branch`` executes and bills it; ``procurement_branch`` buys for it.

    A worksite ``Location`` is created with every project and follows its ``branch``.
    Changes to ``branch``, ``procurement_branch`` or ``status`` bump the organization
    generation (``access.0002``).
    """

    class Status(models.TextChoices):
        ACTIVE = "ACTIVE", _("Active")
        ON_HOLD = "ON_HOLD", _("On hold")
        CLOSED = "CLOSED", _("Closed")

    code = models.CharField(_("code"), max_length=30, unique=True, validators=[code_validator])
    name = models.CharField(_("name"), max_length=200)
    client_name = models.CharField(_("client"), max_length=200, blank=True)
    address = models.TextField(_("address"), blank=True)
    branch = models.ForeignKey(
        Branch, on_delete=models.PROTECT, related_name="projects", verbose_name=_("branch")
    )
    procurement_branch = models.ForeignKey(
        Branch,
        on_delete=models.PROTECT,
        related_name="procured_projects",
        verbose_name=_("procurement branch"),
    )
    status = models.CharField(_("status"), max_length=10, choices=Status, default=Status.ACTIVE)
    start_date = models.DateField(_("start date"), null=True, blank=True)
    end_date = models.DateField(_("end date"), null=True, blank=True)

    objects = ProjectQuerySet.as_manager()

    class Meta:
        verbose_name = _("project")
        verbose_name_plural = _("projects")
        ordering = ["code"]
        constraints = [
            models.CheckConstraint(
                condition=Q(code__regex=CODE_PATTERN), name="projects_project_code_format"
            ),
            models.CheckConstraint(
                condition=Q(status__in=["ACTIVE", "ON_HOLD", "CLOSED"]),
                name="projects_project_status_valid",
            ),
            models.CheckConstraint(
                condition=Q(start_date__isnull=True)
                | Q(end_date__isnull=True)
                | Q(end_date__gte=models.F("start_date")),
                name="projects_project_dates",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.code} · {self.name}"


class ProjectAssignment(BaseModel):
    """Shares one project with a site manager or a partner (plan §3.3, S4).

    At most one non-revoked row per (project, user, kind). Revoked, never deleted; every
    change bumps the user's ``authz_generation`` (``access.0002``).
    """

    class Kind(models.TextChoices):
        MANAGER = "MANAGER", _("Site manager")
        PARTNER = "PARTNER", _("Partner")

    project = models.ForeignKey(
        Project, on_delete=models.PROTECT, related_name="assignments", verbose_name=_("project")
    )
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="project_assignments",
        verbose_name=_("user"),
    )
    kind = models.CharField(_("kind"), max_length=10, choices=Kind)
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
        verbose_name = _("project assignment")
        verbose_name_plural = _("project assignments")
        constraints = [
            models.UniqueConstraint(
                fields=["project", "user", "kind"],
                condition=Q(revoked_at__isnull=True),
                name="projects_assignment_one_open",
            ),
            models.CheckConstraint(
                condition=Q(kind__in=["MANAGER", "PARTNER"]), name="projects_assignment_kind_valid"
            ),
            models.CheckConstraint(
                condition=Q(valid_until__isnull=True) | Q(valid_until__gt=models.F("valid_from")),
                name="projects_assignment_validity",
            ),
            models.CheckConstraint(
                condition=Q(revoked_by__isnull=True) | Q(revoked_at__isnull=False),
                name="projects_assignment_revoked_by_needs_time",
            ),
        ]
        indexes = [models.Index(fields=["user", "kind"], name="projects_assignment_user")]

    def __str__(self) -> str:
        return f"{self.project_id} · {self.user_id} · {self.kind}"

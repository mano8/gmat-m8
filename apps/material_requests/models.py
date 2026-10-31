"""Material requests and their lines (plan §4.2, §4.3, §4.4).

Models hold data and constraints only; every change goes through ``services.py``, whose
state changes are checked against ``transitions.ALLOWED_TRANSITIONS``. Received, pending,
delivered and the other ledger quantities are derived (plan §4.5), never stored here.

Cross-document integrity in the database (``material_requests.0002``):

- C9: ``(material_id, unit_id) → catalog_material(id, unit_id)``, so a line carries its
  material's unit and a referenced material's unit never changes;
- C3: ``(request_id, project_id) → materialrequest(id, project_id)`` on lines, the
  denormalized project that handover lines will reference through ``(id, project_id)``.
  It cascades on update: a draft's lines follow a change of its project.

Scope (S5): a request is a document of its ``procurement_branch`` (set from the project
when the draft is created, taken again at submit) or, for assigned projects, of its
project. Own-reach grants (technicians, partners, own drafts) reach only the user's own
requests, on any project of their branches (S3), so a technician of a managed branch
sees the requests it made for a project its managing branch buys for.
"""

from django.conf import settings
from django.db import models
from django.db.models import F, Q
from django.utils.translation import gettext_lazy as _

from apps.access.roles import Reach
from apps.access.scopes import Scope, ScopedQuerySet, any_of
from apps.catalog.models import Material, UnitOfMeasure
from apps.core.fields import QuantityField
from apps.core.models import BaseModel
from apps.organizations.models import Branch
from apps.projects.models import Project

# S5 for the grants that reach a whole scope; own-reach grants also follow the project.
DOCUMENT_BRANCHES = ("procurement_branch",)
OWN_BRANCHES = ("procurement_branch", "project__branch", "project__procurement_branch")

# For the CHECK constraints (``Meta`` cannot see the nested choices).
STATUSES = ["DRAFT", "SUBMITTED", "APPROVED", "REJECTED", "CANCELLED", "CLOSED"]
PRIORITIES = ["LOW", "NORMAL", "HIGH", "URGENT"]


def request_q(scope: Scope, prefix: str = "") -> Q:
    """Requests in ``scope`` (S5), through ``prefix`` (``"request__"`` for lines)."""
    parts = []
    for reach_is_own in (False, True):
        grants = tuple(g for g in scope.grants if (g.reach is Reach.OWN) is reach_is_own)
        if grants:
            branches = OWN_BRANCHES if reach_is_own else DOCUMENT_BRANCHES
            parts.append(
                Scope(scope.capability, grants, scope.user_id).document_q(
                    branch=tuple(f"{prefix}{name}" for name in branches),
                    project=f"{prefix}project",
                    owner=f"{prefix}requested_by",
                )
            )
    return any_of(parts)


class RequestQuerySet(ScopedQuerySet["MaterialRequest"]):
    def scope_q(self, scope: Scope) -> Q:
        return request_q(scope)


class LineQuerySet(ScopedQuerySet["MaterialRequestLine"]):
    def scope_q(self, scope: Scope) -> Q:
        return request_q(scope, "request__")


class MaterialRequest(BaseModel):
    """A request for materials for one project (plan §4.2).

    ``number`` is assigned at the first submit and kept through a withdrawal; a discarded
    draft never gets one. ``status`` holds approval only: approval (full or partial) and
    fulfilment are derived.
    """

    class Status(models.TextChoices):
        DRAFT = "DRAFT", _("Draft")
        SUBMITTED = "SUBMITTED", _("Submitted")
        APPROVED = "APPROVED", _("Approved")
        REJECTED = "REJECTED", _("Rejected")
        CANCELLED = "CANCELLED", _("Cancelled")
        CLOSED = "CLOSED", _("Closed")

    class Priority(models.TextChoices):
        LOW = "LOW", _("Low")
        NORMAL = "NORMAL", _("Normal")
        HIGH = "HIGH", _("High")
        URGENT = "URGENT", _("Urgent")

    class CloseMode(models.TextChoices):
        AUTO = "AUTO", _("Automatic")
        MANUAL = "MANUAL", _("Manual")

    # NULL until the first submit: unique once set.
    number = models.CharField(
        _("number"), max_length=64, null=True, blank=True, unique=True, editable=False
    )
    project = models.ForeignKey(
        Project, on_delete=models.PROTECT, related_name="requests", verbose_name=_("project")
    )
    procurement_branch = models.ForeignKey(
        Branch,
        on_delete=models.PROTECT,
        related_name="requests",
        verbose_name=_("procurement branch"),
    )
    requested_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="material_requests",
        verbose_name=_("requested by"),
    )
    priority = models.CharField(
        _("priority"), max_length=10, choices=Priority, default=Priority.NORMAL
    )
    needed_by = models.DateField(_("needed by"), null=True, blank=True)
    status = models.CharField(_("status"), max_length=10, choices=Status, default=Status.DRAFT)
    close_mode = models.CharField(_("close mode"), max_length=10, choices=CloseMode, blank=True)
    notes = models.TextField(_("notes"), blank=True)
    submitted_at = models.DateTimeField(_("submitted at"), null=True, blank=True)
    decided_at = models.DateTimeField(_("decided at"), null=True, blank=True)
    closed_at = models.DateTimeField(_("closed at"), null=True, blank=True)
    close_reason = models.TextField(_("close reason"), blank=True)

    objects = RequestQuerySet.as_manager()

    class Meta:
        verbose_name = _("material request")
        verbose_name_plural = _("material requests")
        ordering = ["-created_at"]
        constraints = [
            # The target of the lines' (request_id, project_id) foreign key (C3).
            models.UniqueConstraint(fields=["id", "project"], name="mr_request_id_project"),
            models.CheckConstraint(
                condition=Q(status__in=STATUSES), name="mr_request_status_valid"
            ),
            models.CheckConstraint(
                condition=Q(priority__in=PRIORITIES),
                name="mr_request_priority_valid",
            ),
            models.CheckConstraint(
                condition=Q(status__in=["DRAFT", "CANCELLED"])
                | Q(number__isnull=False, submitted_at__isnull=False),
                name="mr_request_submitted_has_number",
            ),
            models.CheckConstraint(
                condition=Q(number__isnull=True) | Q(submitted_at__isnull=False),
                name="mr_request_number_has_submit_time",
            ),
            models.CheckConstraint(
                condition=(Q(closed_at__isnull=True, close_mode="") & ~Q(status="CLOSED"))
                | Q(status="CLOSED", closed_at__isnull=False, close_mode__in=["AUTO", "MANUAL"]),
                name="mr_request_closure",
            ),
        ]
        indexes = [
            models.Index(fields=["requested_by", "status"], name="mr_request_requester"),
            models.Index(fields=["procurement_branch", "status"], name="mr_request_branch"),
            models.Index(fields=["project", "status"], name="mr_request_project"),
        ]

    def __str__(self) -> str:
        return self.number or f"{self.project_id} · {self.get_status_display()}"


class MaterialRequestLine(BaseModel):
    """One requested material, from the catalog or described in ``free_text``.

    ``project`` is the request's, copied for the composite foreign keys (C3). ``unit`` is
    the material's for a catalog line (C9); a non-catalog line names its own and must be
    linked to a material before it can be purchased.
    """

    class Decision(models.TextChoices):
        PENDING = "PENDING", _("Pending")
        APPROVED = "APPROVED", _("Approved")
        REJECTED = "REJECTED", _("Rejected")

    class Sourcing(models.TextChoices):
        UNDECIDED = "UNDECIDED", _("Undecided")
        STOCK = "STOCK", _("From stock")
        PURCHASE = "PURCHASE", _("Purchase")

    class ClosedShortSource(models.TextChoices):
        LINE = "LINE", _("Line closed")
        REQUEST = "REQUEST", _("Request closed")

    request = models.ForeignKey(
        MaterialRequest, on_delete=models.PROTECT, related_name="lines", verbose_name=_("request")
    )
    project = models.ForeignKey(
        Project, on_delete=models.PROTECT, related_name="+", verbose_name=_("project")
    )
    line_no = models.PositiveSmallIntegerField(_("line"))
    material = models.ForeignKey(
        Material,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="request_lines",
        verbose_name=_("material"),
    )
    free_text = models.CharField(_("description"), max_length=300, blank=True)
    unit = models.ForeignKey(
        UnitOfMeasure, on_delete=models.PROTECT, related_name="+", verbose_name=_("unit")
    )
    requested_qty = QuantityField(_("requested quantity"))
    approved_qty = QuantityField(_("approved quantity"), null=True, blank=True)
    decision = models.CharField(
        _("decision"), max_length=10, choices=Decision, default=Decision.PENDING
    )
    decided_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="+",
        verbose_name=_("decided by"),
    )
    decided_at = models.DateTimeField(_("decided at"), null=True, blank=True)
    decision_note = models.TextField(_("decision note"), blank=True)
    sourcing = models.CharField(
        _("sourcing"), max_length=10, choices=Sourcing, default=Sourcing.UNDECIDED
    )
    closed_short_at = models.DateTimeField(_("closed short at"), null=True, blank=True)
    closed_short_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="+",
        verbose_name=_("closed short by"),
    )
    closed_short_source = models.CharField(
        _("closed by"), max_length=10, choices=ClosedShortSource, blank=True
    )
    close_reason = models.TextField(_("close reason"), blank=True)

    objects = LineQuerySet.as_manager()

    class Meta:
        verbose_name = _("material request line")
        verbose_name_plural = _("material request lines")
        ordering = ["request", "line_no"]
        constraints = [
            models.UniqueConstraint(fields=["request", "line_no"], name="mr_line_number"),
            # The target of the handover lines' (request_line_id, project_id) key (C3, M6).
            models.UniqueConstraint(fields=["id", "project"], name="mr_line_id_project"),
            models.CheckConstraint(condition=Q(line_no__gte=1), name="mr_line_no_positive"),
            models.CheckConstraint(
                condition=Q(requested_qty__gt=0), name="mr_line_requested_positive"
            ),
            models.CheckConstraint(
                condition=Q(approved_qty__isnull=True)
                | Q(approved_qty__gte=0, approved_qty__lte=F("requested_qty")),
                name="mr_line_approved_within_requested",
            ),
            models.CheckConstraint(
                condition=Q(material__isnull=False) | ~Q(free_text=""),
                name="mr_line_material_or_text",
            ),
            models.CheckConstraint(
                condition=Q(decision__in=["PENDING", "APPROVED", "REJECTED"]),
                name="mr_line_decision_valid",
            ),
            models.CheckConstraint(
                condition=Q(
                    decision="PENDING",
                    approved_qty__isnull=True,
                    decided_by__isnull=True,
                    decided_at__isnull=True,
                )
                | Q(decision="APPROVED", approved_qty__isnull=False, decided_at__isnull=False)
                | Q(decision="REJECTED", decided_at__isnull=False),
                name="mr_line_decision_fields",
            ),
            models.CheckConstraint(
                condition=Q(sourcing__in=["UNDECIDED", "STOCK", "PURCHASE"]),
                name="mr_line_sourcing_valid",
            ),
            models.CheckConstraint(
                condition=~Q(sourcing="PURCHASE") | Q(material__isnull=False),
                name="mr_line_purchase_needs_material",
            ),
            models.CheckConstraint(
                condition=Q(
                    closed_short_at__isnull=True,
                    closed_short_by__isnull=True,
                    closed_short_source="",
                    close_reason="",
                )
                | (
                    Q(
                        closed_short_at__isnull=False,
                        closed_short_by__isnull=False,
                        closed_short_source__in=["LINE", "REQUEST"],
                    )
                    & ~Q(close_reason="")
                ),
                name="mr_line_closure_fields",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.request} #{self.line_no}"

    @property
    def description(self) -> str:
        return str(self.material) if self.material_id is not None else self.free_text

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

History of reviewer edits (M7b.3, ADR-0014 §5-§6), append-only by trigger
(``material_requests.0004``): every submit copies the lines to a ``MaterialRequestBaseline``
(the as-submitted copy), and every edit by a ``request.edit`` holder is a
``MaterialRequestLineRevision``. Both refer to a line by its id without a foreign key: a
draft line is deleted when its requester removes it after a return for changes, and the
history of earlier submits must survive that.
"""

from django.conf import settings
from django.core.serializers.json import DjangoJSONEncoder
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
    # The reviewer the request is sent to (M7b.2, Q31): never restricts who decides. A
    # revoked or expired assignee reads as unassigned (``reviewers.assignee``); the row
    # keeps them.
    assigned_reviewer = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="assigned_requests",
        verbose_name=_("assigned reviewer"),
    )
    assigned_at = models.DateTimeField(_("assigned at"), null=True, blank=True)
    assigned_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="+",
        verbose_name=_("assigned by"),
    )

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
            models.CheckConstraint(
                condition=Q(
                    assigned_reviewer__isnull=True,
                    assigned_at__isnull=True,
                    assigned_by__isnull=True,
                )
                | Q(
                    assigned_reviewer__isnull=False,
                    assigned_at__isnull=False,
                    assigned_by__isnull=False,
                ),
                name="mr_request_assignment",
            ),
        ]
        indexes = [
            models.Index(fields=["requested_by", "status"], name="mr_request_requester"),
            models.Index(fields=["procurement_branch", "status"], name="mr_request_branch"),
            models.Index(fields=["project", "status"], name="mr_request_project"),
            models.Index(fields=["assigned_reviewer", "status"], name="mr_request_assignee"),
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
            # The target of the handover lines' (request_line_id, project_id) key (C3).
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


class BaselineQuerySet(ScopedQuerySet["MaterialRequestBaseline"]):
    def scope_q(self, scope: Scope) -> Q:
        return request_q(scope, "request__")


class BaselineLineQuerySet(ScopedQuerySet["MaterialRequestBaselineLine"]):
    def scope_q(self, scope: Scope) -> Q:
        return request_q(scope, "baseline__request__")


class RevisionQuerySet(ScopedQuerySet["MaterialRequestLineRevision"]):
    def scope_q(self, scope: Scope) -> Q:
        return request_q(scope, "request__")


class MaterialRequestBaseline(BaseModel):
    """The request as its requester submitted it, once per submit (M7b.3, Q34).

    ``sequence`` counts the submits of the request (1 for the first); a resubmit after a
    return to draft starts a new baseline and the earlier ones stay. ``backfilled``: the
    request was submitted before baselines existed, so its first reviewer edit copied the
    lines as they were then. Append-only (trigger).
    """

    request = models.ForeignKey(
        MaterialRequest,
        on_delete=models.PROTECT,
        related_name="baselines",
        verbose_name=_("request"),
    )
    sequence = models.PositiveSmallIntegerField(_("submit"))
    submitted_at = models.DateTimeField(_("submitted at"))
    submitted_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="+",
        verbose_name=_("submitted by"),
    )
    notes = models.TextField(_("notes"), blank=True)
    backfilled = models.BooleanField(_("recorded at the first edit"), default=False)

    objects = BaselineQuerySet.as_manager()

    class Meta:
        verbose_name = _("as-submitted copy")
        verbose_name_plural = _("as-submitted copies")
        ordering = ["request", "sequence"]
        constraints = [
            models.UniqueConstraint(fields=["request", "sequence"], name="mr_baseline_sequence"),
            models.CheckConstraint(
                condition=Q(sequence__gte=1), name="mr_baseline_sequence_positive"
            ),
        ]

    def __str__(self) -> str:
        return f"{self.request} · {self.sequence}"


class MaterialRequestBaselineLine(BaseModel):
    """One line of a baseline: material, unit, description and quantity as submitted.

    ``line_id`` and ``line_no`` name the request line (no foreign key, see the module
    note). ``material_snapshot`` keeps the catalog reference and name of that day. The
    unit is the material's (C9, ``material_requests.0004``).
    """

    baseline = models.ForeignKey(
        MaterialRequestBaseline,
        on_delete=models.PROTECT,
        related_name="lines",
        verbose_name=_("as-submitted copy"),
    )
    line_id = models.UUIDField(_("line id"))
    line_no = models.PositiveSmallIntegerField(_("line"))
    material = models.ForeignKey(
        Material,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="+",
        verbose_name=_("material"),
    )
    material_snapshot = models.JSONField(
        _("material (as submitted)"), default=dict, blank=True, encoder=DjangoJSONEncoder
    )
    free_text = models.CharField(_("description"), max_length=300, blank=True)
    unit = models.ForeignKey(
        UnitOfMeasure, on_delete=models.PROTECT, related_name="+", verbose_name=_("unit")
    )
    requested_qty = QuantityField(_("requested quantity"))

    objects = BaselineLineQuerySet.as_manager()

    class Meta:
        verbose_name = _("as-submitted line")
        verbose_name_plural = _("as-submitted lines")
        ordering = ["baseline", "line_no"]
        constraints = [
            models.UniqueConstraint(fields=["baseline", "line_id"], name="mr_baseline_line"),
            models.CheckConstraint(
                condition=Q(requested_qty__gt=0), name="mr_baseline_line_qty_positive"
            ),
            models.CheckConstraint(
                condition=Q(material__isnull=False) | ~Q(free_text=""),
                name="mr_baseline_line_material_or_text",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.baseline} #{self.line_no}"


class MaterialRequestLineRevision(BaseModel):
    """One edit of a request line by a ``request.edit`` holder (M7b.3, Q32-Q34, Q36).

    ``before`` and ``after`` hold the line's values (``revisions.values``): ``before`` is
    empty for an added line; a replacement's ``after`` is the new line, named by
    ``replacement_line_id``. ``self_approved``: the editor decided the edited line
    themselves (always in ``APPROVED``; in ``SUBMITTED`` when they approved or rejected it
    in the same step, a removal included) instead of leaving it to another reviewer.
    Append-only (trigger).
    """

    class Kind(models.TextChoices):
        CHANGE = "CHANGE", _("Changed")
        ADD = "ADD", _("Added")
        REMOVE = "REMOVE", _("Removed")
        REPLACE = "REPLACE", _("Replaced")

    request = models.ForeignKey(
        MaterialRequest,
        on_delete=models.PROTECT,
        related_name="revisions",
        verbose_name=_("request"),
    )
    baseline = models.ForeignKey(
        MaterialRequestBaseline,
        on_delete=models.PROTECT,
        related_name="revisions",
        verbose_name=_("as-submitted copy"),
    )
    line_id = models.UUIDField(_("line id"))
    line_no = models.PositiveSmallIntegerField(_("line"))
    kind = models.CharField(_("kind"), max_length=10, choices=Kind)
    editor = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="+",
        verbose_name=_("edited by"),
    )
    reason = models.TextField(_("reason"))
    before = models.JSONField(_("before"), default=dict, blank=True, encoder=DjangoJSONEncoder)
    after = models.JSONField(_("after"), default=dict, blank=True, encoder=DjangoJSONEncoder)
    self_approved = models.BooleanField(_("approved by the editor"), default=False)
    replacement_line_id = models.UUIDField(_("replacement line id"), null=True, blank=True)

    objects = RevisionQuerySet.as_manager()

    class Meta:
        verbose_name = _("line revision")
        verbose_name_plural = _("line revisions")
        ordering = ["request", "created_at", "id"]
        constraints = [
            models.CheckConstraint(
                condition=Q(kind__in=["CHANGE", "ADD", "REMOVE", "REPLACE"]),
                name="mr_revision_kind_valid",
            ),
            models.CheckConstraint(condition=~Q(reason=""), name="mr_revision_reason"),
            models.CheckConstraint(
                condition=Q(kind="REPLACE", replacement_line_id__isnull=False)
                | (~Q(kind="REPLACE") & Q(replacement_line_id__isnull=True)),
                name="mr_revision_replacement",
            ),
        ]
        indexes = [
            models.Index(fields=["request", "created_at"], name="mr_revision_request"),
        ]

    def __str__(self) -> str:
        return f"{self.request} #{self.line_no} · {self.get_kind_display()}"

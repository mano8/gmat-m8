"""Places where goods arrive: warehouses and worksites (plan §3.3, §4.10).

``Location`` becomes the stock location of business phase 2; ``VEHICLE`` comes later (Q16).
"""

from django.db import models
from django.db.models import Q
from django.utils.translation import gettext_lazy as _

from apps.core.models import BaseModel
from apps.organizations.models import Branch


class Location(BaseModel):
    """``project`` is set if and only if ``kind = WORKSITE``; one worksite per project.

    A branch has 0..n warehouses, one of them its ``default_warehouse`` (Q3). A worksite
    belongs to its project's ``branch`` and moves with it.
    """

    class Kind(models.TextChoices):
        WAREHOUSE = "WAREHOUSE", _("Warehouse")
        WORKSITE = "WORKSITE", _("Worksite")

    kind = models.CharField(_("kind"), max_length=10, choices=Kind)
    branch = models.ForeignKey(
        Branch, on_delete=models.PROTECT, related_name="locations", verbose_name=_("branch")
    )
    project = models.OneToOneField(
        "projects.Project",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="worksite",
        verbose_name=_("project"),
    )
    name = models.CharField(_("name"), max_length=200)
    address = models.TextField(_("address"), blank=True)
    is_active = models.BooleanField(_("active"), default=True)

    class Meta:
        verbose_name = _("location")
        verbose_name_plural = _("locations")
        ordering = ["branch_id", "kind", "name"]
        constraints = [
            models.CheckConstraint(
                condition=Q(kind__in=["WAREHOUSE", "WORKSITE"]),
                name="locations_location_kind_valid",
            ),
            models.CheckConstraint(
                condition=Q(kind="WORKSITE", project__isnull=False)
                | (~Q(kind="WORKSITE") & Q(project__isnull=True)),
                name="locations_location_project_iff_worksite",
            ),
            # Target of the branch's (default_warehouse_id, id) foreign key: a default
            # warehouse belongs to its own branch.
            models.UniqueConstraint(fields=["id", "branch"], name="locations_location_id_branch"),
        ]

    def __str__(self) -> str:
        return self.name

"""The as-submitted copy and the reviewer's line revisions (M7b.3, ADR-0014 §5-§6).

Expand only: three new tables the previous release ignores. A request submitted by that
release has no baseline; its first reviewer edit records one from the lines as they are
then (``backfilled``).

- C9 on the copy: ``(material_id, unit_id) → catalog_material(id, unit_id)``, like the
  request lines (``MATCH SIMPLE`` skips a non-catalog line);
- append-only: a row trigger refuses every ``UPDATE`` and ``DELETE`` of the three tables
  with ``restrict_violation`` (an ``IntegrityError``), whatever the code path.

Reversible: the reverse drops the triggers, their function and the tables.
"""

import apps.core.fields
import apps.core.ids
import django.core.serializers.json
import django.db.models.deletion
import django.utils.timezone
from django.conf import settings
from django.db import migrations, models

from apps.core.migration_ops import AddCompositeForeignKey

TABLES = (
    "material_requests_materialrequestbaseline",
    "material_requests_materialrequestbaselineline",
    "material_requests_materialrequestlinerevision",
)

FORWARD = """
CREATE FUNCTION material_requests_history_append_only() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION '% rows are append-only: % refused', TG_TABLE_NAME, TG_OP
        USING ERRCODE = 'restrict_violation';
END;
$$;
""" + "".join(
    f"""
CREATE TRIGGER {table}_append_only
BEFORE UPDATE OR DELETE ON {table}
FOR EACH ROW EXECUTE FUNCTION material_requests_history_append_only();
"""
    for table in TABLES
)

REVERSE = (
    "".join(f"DROP TRIGGER IF EXISTS {table}_append_only ON {table};\n" for table in TABLES)
    + "DROP FUNCTION IF EXISTS material_requests_history_append_only();\n"
)


class Migration(migrations.Migration):
    dependencies = [
        ("catalog", "0003_units_of_measure"),
        ("material_requests", "0003_reviewer_assignment"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name="MaterialRequestBaseline",
            fields=[
                (
                    "id",
                    models.UUIDField(
                        default=apps.core.ids.new_id,
                        editable=False,
                        primary_key=True,
                        serialize=False,
                    ),
                ),
                (
                    "created_at",
                    models.DateTimeField(default=django.utils.timezone.now, editable=False),
                ),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("sequence", models.PositiveSmallIntegerField(verbose_name="submit")),
                ("submitted_at", models.DateTimeField(verbose_name="submitted at")),
                ("notes", models.TextField(blank=True, verbose_name="notes")),
                (
                    "backfilled",
                    models.BooleanField(default=False, verbose_name="recorded at the first edit"),
                ),
                (
                    "request",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="baselines",
                        to="material_requests.materialrequest",
                        verbose_name="request",
                    ),
                ),
                (
                    "submitted_by",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="+",
                        to=settings.AUTH_USER_MODEL,
                        verbose_name="submitted by",
                    ),
                ),
            ],
            options={
                "verbose_name": "as-submitted copy",
                "verbose_name_plural": "as-submitted copies",
                "ordering": ["request", "sequence"],
            },
        ),
        migrations.CreateModel(
            name="MaterialRequestBaselineLine",
            fields=[
                (
                    "id",
                    models.UUIDField(
                        default=apps.core.ids.new_id,
                        editable=False,
                        primary_key=True,
                        serialize=False,
                    ),
                ),
                (
                    "created_at",
                    models.DateTimeField(default=django.utils.timezone.now, editable=False),
                ),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("line_id", models.UUIDField(verbose_name="line id")),
                ("line_no", models.PositiveSmallIntegerField(verbose_name="line")),
                (
                    "material_snapshot",
                    models.JSONField(
                        blank=True,
                        default=dict,
                        encoder=django.core.serializers.json.DjangoJSONEncoder,
                        verbose_name="material (as submitted)",
                    ),
                ),
                (
                    "free_text",
                    models.CharField(blank=True, max_length=300, verbose_name="description"),
                ),
                (
                    "requested_qty",
                    apps.core.fields.QuantityField(verbose_name="requested quantity"),
                ),
                (
                    "baseline",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="lines",
                        to="material_requests.materialrequestbaseline",
                        verbose_name="as-submitted copy",
                    ),
                ),
                (
                    "material",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="+",
                        to="catalog.material",
                        verbose_name="material",
                    ),
                ),
                (
                    "unit",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="+",
                        to="catalog.unitofmeasure",
                        verbose_name="unit",
                    ),
                ),
            ],
            options={
                "verbose_name": "as-submitted line",
                "verbose_name_plural": "as-submitted lines",
                "ordering": ["baseline", "line_no"],
            },
        ),
        migrations.CreateModel(
            name="MaterialRequestLineRevision",
            fields=[
                (
                    "id",
                    models.UUIDField(
                        default=apps.core.ids.new_id,
                        editable=False,
                        primary_key=True,
                        serialize=False,
                    ),
                ),
                (
                    "created_at",
                    models.DateTimeField(default=django.utils.timezone.now, editable=False),
                ),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("line_id", models.UUIDField(verbose_name="line id")),
                ("line_no", models.PositiveSmallIntegerField(verbose_name="line")),
                (
                    "kind",
                    models.CharField(
                        choices=[
                            ("CHANGE", "Changed"),
                            ("ADD", "Added"),
                            ("REMOVE", "Removed"),
                            ("REPLACE", "Replaced"),
                        ],
                        max_length=10,
                        verbose_name="kind",
                    ),
                ),
                ("reason", models.TextField(verbose_name="reason")),
                (
                    "before",
                    models.JSONField(
                        blank=True,
                        default=dict,
                        encoder=django.core.serializers.json.DjangoJSONEncoder,
                        verbose_name="before",
                    ),
                ),
                (
                    "after",
                    models.JSONField(
                        blank=True,
                        default=dict,
                        encoder=django.core.serializers.json.DjangoJSONEncoder,
                        verbose_name="after",
                    ),
                ),
                (
                    "self_approved",
                    models.BooleanField(default=False, verbose_name="approved by the editor"),
                ),
                (
                    "replacement_line_id",
                    models.UUIDField(blank=True, null=True, verbose_name="replacement line id"),
                ),
                (
                    "baseline",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="revisions",
                        to="material_requests.materialrequestbaseline",
                        verbose_name="as-submitted copy",
                    ),
                ),
                (
                    "editor",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="+",
                        to=settings.AUTH_USER_MODEL,
                        verbose_name="edited by",
                    ),
                ),
                (
                    "request",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="revisions",
                        to="material_requests.materialrequest",
                        verbose_name="request",
                    ),
                ),
            ],
            options={
                "verbose_name": "line revision",
                "verbose_name_plural": "line revisions",
                "ordering": ["request", "created_at", "id"],
            },
        ),
        migrations.AddConstraint(
            model_name="materialrequestbaseline",
            constraint=models.UniqueConstraint(
                fields=("request", "sequence"), name="mr_baseline_sequence"
            ),
        ),
        migrations.AddConstraint(
            model_name="materialrequestbaseline",
            constraint=models.CheckConstraint(
                condition=models.Q(("sequence__gte", 1)), name="mr_baseline_sequence_positive"
            ),
        ),
        migrations.AddConstraint(
            model_name="materialrequestbaselineline",
            constraint=models.UniqueConstraint(
                fields=("baseline", "line_id"), name="mr_baseline_line"
            ),
        ),
        migrations.AddConstraint(
            model_name="materialrequestbaselineline",
            constraint=models.CheckConstraint(
                condition=models.Q(("requested_qty__gt", 0)), name="mr_baseline_line_qty_positive"
            ),
        ),
        migrations.AddConstraint(
            model_name="materialrequestbaselineline",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    ("material__isnull", False),
                    models.Q(("free_text", ""), _negated=True),
                    _connector="OR",
                ),
                name="mr_baseline_line_material_or_text",
            ),
        ),
        migrations.AddIndex(
            model_name="materialrequestlinerevision",
            index=models.Index(fields=["request", "created_at"], name="mr_revision_request"),
        ),
        migrations.AddConstraint(
            model_name="materialrequestlinerevision",
            constraint=models.CheckConstraint(
                condition=models.Q(("kind__in", ["CHANGE", "ADD", "REMOVE", "REPLACE"])),
                name="mr_revision_kind_valid",
            ),
        ),
        migrations.AddConstraint(
            model_name="materialrequestlinerevision",
            constraint=models.CheckConstraint(
                condition=models.Q(("reason", ""), _negated=True), name="mr_revision_reason"
            ),
        ),
        migrations.AddConstraint(
            model_name="materialrequestlinerevision",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    models.Q(("kind", "REPLACE"), ("replacement_line_id__isnull", False)),
                    models.Q(
                        models.Q(("kind", "REPLACE"), _negated=True),
                        ("replacement_line_id__isnull", True),
                    ),
                    _connector="OR",
                ),
                name="mr_revision_replacement",
            ),
        ),
        AddCompositeForeignKey(
            name="mr_baseline_line_material_unit_fk",
            table="material_requests_materialrequestbaselineline",
            columns=("material_id", "unit_id"),
            ref_table="catalog_material",
            ref_columns=("id", "unit_id"),
        ),
        migrations.RunSQL(FORWARD, REVERSE),
    ]

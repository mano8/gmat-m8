"""Composite foreign keys of request lines (plan §4.6).

- C9: ``(material_id, unit_id) → catalog_material(id, unit_id)``. A line carries its
  material's unit; changing a referenced material's unit fails, and the first reference
  racing a unit change is serialized by the key's ``FOR KEY SHARE`` lock. ``MATCH SIMPLE``
  skips non-catalog lines (no material).
- C3: ``(request_id, project_id) → materialrequest(id, project_id)``: the denormalized
  project is the request's. It cascades on update, so a draft's lines follow a change of
  its project.
"""

from django.db import migrations

from apps.core.migration_ops import AddCompositeForeignKey


class Migration(migrations.Migration):
    dependencies = [
        ("catalog", "0003_units_of_measure"),
        ("material_requests", "0001_initial"),
    ]

    operations = [
        AddCompositeForeignKey(
            name="mr_line_material_unit_fk",
            table="material_requests_materialrequestline",
            columns=("material_id", "unit_id"),
            ref_table="catalog_material",
            ref_columns=("id", "unit_id"),
        ),
        AddCompositeForeignKey(
            name="mr_line_request_project_fk",
            table="material_requests_materialrequestline",
            columns=("request_id", "project_id"),
            ref_table="material_requests_materialrequest",
            ref_columns=("id", "project_id"),
            on_update="CASCADE",
        ),
    ]

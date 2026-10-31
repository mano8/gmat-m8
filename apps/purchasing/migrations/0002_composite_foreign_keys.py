"""Composite foreign key of purchase-order lines (plan §4.6).

C9: ``(material_id, unit_id) → catalog_material(id, unit_id)``. A line carries its
material's unit; changing a referenced material's unit fails, and a first reference racing
a unit change is serialized by the key's ``FOR KEY SHARE`` lock.
"""

from django.db import migrations

from apps.core.migration_ops import AddCompositeForeignKey


class Migration(migrations.Migration):
    dependencies = [
        ("catalog", "0003_units_of_measure"),
        ("purchasing", "0001_initial"),
    ]

    operations = [
        AddCompositeForeignKey(
            name="po_line_material_unit_fk",
            table="purchasing_purchaseorderline",
            columns=("material_id", "unit_id"),
            ref_table="catalog_material",
            ref_columns=("id", "unit_id"),
        ),
    ]

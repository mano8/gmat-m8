"""A unit of measure can be withdrawn from the choices for new materials (Q72, M10.3).

Expand only: a new column with a default, so every existing unit stays active and the previous
release keeps working against the new schema. Reversing drops the column.
"""

from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("catalog", "0003_units_of_measure")]

    operations = [
        migrations.AddField(
            model_name="unitofmeasure",
            name="is_active",
            field=models.BooleanField(default=True, verbose_name="active"),
        ),
    ]

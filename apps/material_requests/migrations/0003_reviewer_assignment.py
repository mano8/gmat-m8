"""Reviewer assignment of a request (M7b.2, Q31, ADR-0014 §4).

Expand only: three nullable columns, their all-or-nothing CHECK and the index of the
"Assigned to me" list. The previous release ignores the columns. Reversible: going back
drops the assignments (the audit keeps who was assigned).
"""

import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("material_requests", "0002_composite_foreign_keys"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.AddField(
            model_name="materialrequest",
            name="assigned_at",
            field=models.DateTimeField(blank=True, null=True, verbose_name="assigned at"),
        ),
        migrations.AddField(
            model_name="materialrequest",
            name="assigned_by",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="+",
                to=settings.AUTH_USER_MODEL,
                verbose_name="assigned by",
            ),
        ),
        migrations.AddField(
            model_name="materialrequest",
            name="assigned_reviewer",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="assigned_requests",
                to=settings.AUTH_USER_MODEL,
                verbose_name="assigned reviewer",
            ),
        ),
        migrations.AddIndex(
            model_name="materialrequest",
            index=models.Index(fields=["assigned_reviewer", "status"], name="mr_request_assignee"),
        ),
        migrations.AddConstraint(
            model_name="materialrequest",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    models.Q(
                        ("assigned_at__isnull", True),
                        ("assigned_by__isnull", True),
                        ("assigned_reviewer__isnull", True),
                    ),
                    models.Q(
                        ("assigned_at__isnull", False),
                        ("assigned_by__isnull", False),
                        ("assigned_reviewer__isnull", False),
                    ),
                    _connector="OR",
                ),
                name="mr_request_assignment",
            ),
        ),
    ]

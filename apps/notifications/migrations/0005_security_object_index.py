"""The delivery badges (M7c.1): the latest security mail about an invitation or email change.

Built ``CONCURRENTLY`` (outside a transaction) so that queueing and sending mail go on while
the index builds. Expand only; the reverse drops it the same way.
"""

from django.contrib.postgres.operations import AddIndexConcurrently
from django.db import migrations, models


class Migration(migrations.Migration):
    atomic = False

    dependencies = [
        ("notifications", "0004_notification_preference"),
        ("purchasing", "0002_composite_foreign_keys"),
    ]

    operations = [
        AddIndexConcurrently(
            model_name="emailoutbox",
            index=models.Index(
                condition=models.Q(("kind", "SECURITY")),
                fields=["eligibility_object_id", "-created_at"],
                name="notifications_outbox_secobj",
            ),
        ),
    ]

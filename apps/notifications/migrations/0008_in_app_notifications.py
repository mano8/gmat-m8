"""In-app notifications (M17.3, Q94, Q98, Q99).

Expand only: one new table with its indexes (the bell, the list, the purge) and a partial unique
constraint (one unread row per person and request for a grouped notice). Reversible: the reverse
drops the table.
"""

import apps.core.ids
import django.db.models.deletion
import django.db.models.functions.datetime
import django.utils.timezone
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('notifications', '0007_notice_choices'),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name='InAppNotification',
            fields=[
                ('id', models.UUIDField(default=apps.core.ids.new_id, editable=False, primary_key=True, serialize=False)),
                ('created_at', models.DateTimeField(default=django.utils.timezone.now, editable=False)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('notice', models.CharField(choices=[('REQUEST_TO_REVIEW', 'A request to review'), ('REQUEST_ASSIGNED', 'A request assigned to me'), ('REQUEST_APPROVED', 'My request approved'), ('REQUEST_REJECTED', 'My request rejected'), ('REQUEST_EDITED', 'My request changed by a reviewer'), ('REQUEST_HANDED_OVER', 'Goods handed over or received on site')], max_length=40, verbose_name='notice')),
                ('object_type', models.CharField(max_length=100, verbose_name='object type')),
                ('object_id', models.UUIDField(verbose_name='object id')),
                ('notified_at', models.DateTimeField(db_default=django.db.models.functions.datetime.Now(), verbose_name='notified at')),
                ('read_at', models.DateTimeField(blank=True, null=True, verbose_name='read at')),
                ('user', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='in_app_notifications', to=settings.AUTH_USER_MODEL, verbose_name='user')),
            ],
            options={
                'verbose_name': 'in-app notification',
                'verbose_name_plural': 'in-app notifications',
                'indexes': [models.Index(fields=['user', 'read_at', 'notified_at'], name='notif_inapp_user_read_idx'), models.Index(fields=['user', '-notified_at', '-id'], name='notif_inapp_user_list_idx'), models.Index(condition=models.Q(('read_at__isnull', True)), fields=['notified_at'], name='notif_inapp_unread_age_idx'), models.Index(condition=models.Q(('read_at__isnull', False)), fields=['read_at'], name='notif_inapp_read_age_idx')],
                'constraints': [models.UniqueConstraint(condition=models.Q(('notice__in', ('REQUEST_EDITED',)), ('read_at__isnull', True)), fields=('user', 'notice', 'object_type', 'object_id'), name='notifications_inapp_one_unread_grouped')],
            },
        ),
    ]

"""The person's own choices and their switch per channel (M17.1, Q93, Q95, Q96).

Expand only: two new tables (``NoticeChoice``, ``ChannelSwitch``) and a new choice
(``IN_APP``) on the channel of the administrators' settings, which changes no column. Email's
switch stays ``NotificationPreference.email_opt_out``, which the previous release reads and
writes. Reversible: the reverse drops the two tables.
"""
import apps.core.ids
import django.db.models.deletion
import django.db.models.functions.datetime
import django.utils.timezone
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('notifications', '0006_notice_settings'),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.AlterField(
            model_name='noticedefault',
            name='channel',
            field=models.CharField(choices=[('EMAIL', 'Email'), ('IN_APP', 'In the application')], max_length=20, verbose_name='channel'),
        ),
        migrations.AlterField(
            model_name='noticesetting',
            name='channel',
            field=models.CharField(choices=[('EMAIL', 'Email'), ('IN_APP', 'In the application')], max_length=20, verbose_name='channel'),
        ),
        migrations.CreateModel(
            name='ChannelSwitch',
            fields=[
                ('id', models.UUIDField(default=apps.core.ids.new_id, editable=False, primary_key=True, serialize=False)),
                ('created_at', models.DateTimeField(default=django.utils.timezone.now, editable=False)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('channel', models.CharField(choices=[('EMAIL', 'Email'), ('IN_APP', 'In the application')], max_length=20, verbose_name='channel')),
                ('enabled', models.BooleanField(verbose_name='channel on')),
                ('changed_at', models.DateTimeField(db_default=django.db.models.functions.datetime.Now(), verbose_name='changed at')),
                ('user', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='channel_switches', to=settings.AUTH_USER_MODEL, verbose_name='user')),
            ],
            options={
                'verbose_name': 'channel switch',
                'verbose_name_plural': 'channel switches',
                'constraints': [models.UniqueConstraint(fields=('user', 'channel'), name='notifications_channelswitch_one_per_user'), models.CheckConstraint(condition=models.Q(('channel', 'EMAIL'), _negated=True), name='notifications_channelswitch_not_email')],
            },
        ),
        migrations.CreateModel(
            name='NoticeChoice',
            fields=[
                ('id', models.UUIDField(default=apps.core.ids.new_id, editable=False, primary_key=True, serialize=False)),
                ('created_at', models.DateTimeField(default=django.utils.timezone.now, editable=False)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('notice', models.CharField(choices=[('REQUEST_TO_REVIEW', 'A request to review'), ('REQUEST_ASSIGNED', 'A request assigned to me'), ('REQUEST_APPROVED', 'My request approved'), ('REQUEST_REJECTED', 'My request rejected'), ('REQUEST_EDITED', 'My request changed by a reviewer'), ('REQUEST_HANDED_OVER', 'Goods handed over or received on site')], max_length=40, verbose_name='notice')),
                ('channel', models.CharField(choices=[('EMAIL', 'Email'), ('IN_APP', 'In the application')], max_length=20, verbose_name='channel')),
                ('enabled', models.BooleanField(verbose_name='wanted')),
                ('changed_at', models.DateTimeField(db_default=django.db.models.functions.datetime.Now(), verbose_name='changed at')),
                ('user', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='notice_choices', to=settings.AUTH_USER_MODEL, verbose_name='user')),
            ],
            options={
                'verbose_name': 'own notice choice',
                'verbose_name_plural': 'own notice choices',
                'constraints': [models.UniqueConstraint(fields=('user', 'notice', 'channel'), name='notifications_noticechoice_one_per_user')],
            },
        ),
    ]

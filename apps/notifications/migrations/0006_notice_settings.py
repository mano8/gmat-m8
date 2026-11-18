"""Business notices and their settings (M15.1, Q87-Q90).

Expand only: two new tables (``NoticeDefault``, ``NoticeSetting``) and one column,
``EmailOutbox.notice``, blank by a database default so code that does not know it keeps
inserting rows. The outbox's frozen-content trigger (``0002``) is replaced by one that also
freezes ``notice``: which notice a queued message is decides who may still receive it.

Reversible: the reverse puts back the trigger function of ``0002``, then drops the column and
the tables.
"""

import apps.core.ids
import django.db.models.deletion
import django.db.models.functions.datetime
import django.utils.timezone
from django.conf import settings
from django.db import migrations, models


FROZEN_WITH_NOTICE = """
CREATE OR REPLACE FUNCTION notifications_outbox_frozen() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF NEW.kind IS DISTINCT FROM OLD.kind
        OR NEW.sensitive IS DISTINCT FROM OLD.sensitive
        OR NEW.recipient_users IS DISTINCT FROM OLD.recipient_users
        OR NEW.recipient_address IS DISTINCT FROM OLD.recipient_address
        OR NEW.eligibility_rule IS DISTINCT FROM OLD.eligibility_rule
        OR NEW.eligibility_object_type IS DISTINCT FROM OLD.eligibility_object_type
        OR NEW.eligibility_object_id IS DISTINCT FROM OLD.eligibility_object_id
        OR NEW.source_po IS DISTINCT FROM OLD.source_po
        OR NEW.source_ref IS DISTINCT FROM OLD.source_ref
        OR NEW.notice IS DISTINCT FROM OLD.notice
        OR NEW.from_email IS DISTINCT FROM OLD.from_email
        OR NEW.subject IS DISTINCT FROM OLD.subject
        OR NEW.expires_at IS DISTINCT FROM OLD.expires_at
        OR NEW.created_at IS DISTINCT FROM OLD.created_at
        OR (NEW.body IS DISTINCT FROM OLD.body AND NEW.body <> '')
        OR (NEW.html_body IS DISTINCT FROM OLD.html_body AND NEW.html_body <> '')
        OR (OLD.wiped_at IS NOT NULL AND NEW.wiped_at IS DISTINCT FROM OLD.wiped_at)
        OR (OLD.message_id <> '' AND NEW.message_id IS DISTINCT FROM OLD.message_id)
    THEN
        RAISE EXCEPTION 'outbox content is frozen at insert'
            USING ERRCODE = 'restrict_violation';
    END IF;
    RETURN NEW;
END;
$$;
"""

FROZEN_BEFORE = """
CREATE OR REPLACE FUNCTION notifications_outbox_frozen() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF NEW.kind IS DISTINCT FROM OLD.kind
        OR NEW.sensitive IS DISTINCT FROM OLD.sensitive
        OR NEW.recipient_users IS DISTINCT FROM OLD.recipient_users
        OR NEW.recipient_address IS DISTINCT FROM OLD.recipient_address
        OR NEW.eligibility_rule IS DISTINCT FROM OLD.eligibility_rule
        OR NEW.eligibility_object_type IS DISTINCT FROM OLD.eligibility_object_type
        OR NEW.eligibility_object_id IS DISTINCT FROM OLD.eligibility_object_id
        OR NEW.source_po IS DISTINCT FROM OLD.source_po
        OR NEW.source_ref IS DISTINCT FROM OLD.source_ref
        OR NEW.from_email IS DISTINCT FROM OLD.from_email
        OR NEW.subject IS DISTINCT FROM OLD.subject
        OR NEW.expires_at IS DISTINCT FROM OLD.expires_at
        OR NEW.created_at IS DISTINCT FROM OLD.created_at
        OR (NEW.body IS DISTINCT FROM OLD.body AND NEW.body <> '')
        OR (NEW.html_body IS DISTINCT FROM OLD.html_body AND NEW.html_body <> '')
        OR (OLD.wiped_at IS NOT NULL AND NEW.wiped_at IS DISTINCT FROM OLD.wiped_at)
        OR (OLD.message_id <> '' AND NEW.message_id IS DISTINCT FROM OLD.message_id)
    THEN
        RAISE EXCEPTION 'outbox content is frozen at insert'
            USING ERRCODE = 'restrict_violation';
    END IF;
    RETURN NEW;
END;
$$;
"""


class Migration(migrations.Migration):
    dependencies = [
        ("notifications", "0005_security_object_index"),
        ("purchasing", "0002_composite_foreign_keys"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name="NoticeDefault",
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
                (
                    "notice",
                    models.CharField(
                        choices=[
                            ("REQUEST_TO_REVIEW", "A request to review"),
                            ("REQUEST_ASSIGNED", "A request assigned to me"),
                            ("REQUEST_APPROVED", "My request approved"),
                            ("REQUEST_REJECTED", "My request rejected"),
                            ("REQUEST_EDITED", "My request changed by a reviewer"),
                            ("REQUEST_HANDED_OVER", "Goods handed over or received on site"),
                        ],
                        max_length=40,
                        verbose_name="notice",
                    ),
                ),
                (
                    "channel",
                    models.CharField(
                        choices=[("EMAIL", "Email")], max_length=20, verbose_name="channel"
                    ),
                ),
                ("enabled", models.BooleanField(default=True, verbose_name="sent to everyone")),
                (
                    "changed_at",
                    models.DateTimeField(
                        db_default=django.db.models.functions.datetime.Now(),
                        verbose_name="changed at",
                    ),
                ),
            ],
            options={
                "verbose_name": "notice setting for everyone",
                "verbose_name_plural": "notice settings for everyone",
            },
        ),
        migrations.CreateModel(
            name="NoticeSetting",
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
                (
                    "notice",
                    models.CharField(
                        choices=[
                            ("REQUEST_TO_REVIEW", "A request to review"),
                            ("REQUEST_ASSIGNED", "A request assigned to me"),
                            ("REQUEST_APPROVED", "My request approved"),
                            ("REQUEST_REJECTED", "My request rejected"),
                            ("REQUEST_EDITED", "My request changed by a reviewer"),
                            ("REQUEST_HANDED_OVER", "Goods handed over or received on site"),
                        ],
                        max_length=40,
                        verbose_name="notice",
                    ),
                ),
                (
                    "channel",
                    models.CharField(
                        choices=[("EMAIL", "Email")], max_length=20, verbose_name="channel"
                    ),
                ),
                (
                    "mode",
                    models.CharField(
                        choices=[("ALWAYS", "Always"), ("NEVER", "Never")],
                        max_length=10,
                        verbose_name="setting",
                    ),
                ),
                (
                    "set_at",
                    models.DateTimeField(
                        db_default=django.db.models.functions.datetime.Now(), verbose_name="set at"
                    ),
                ),
            ],
            options={
                "verbose_name": "notice setting for a person",
                "verbose_name_plural": "notice settings for a person",
            },
        ),
        migrations.AddField(
            model_name="emailoutbox",
            name="notice",
            field=models.CharField(
                blank=True,
                choices=[
                    ("REQUEST_TO_REVIEW", "A request to review"),
                    ("REQUEST_ASSIGNED", "A request assigned to me"),
                    ("REQUEST_APPROVED", "My request approved"),
                    ("REQUEST_REJECTED", "My request rejected"),
                    ("REQUEST_EDITED", "My request changed by a reviewer"),
                    ("REQUEST_HANDED_OVER", "Goods handed over or received on site"),
                ],
                db_default="",
                max_length=40,
            ),
        ),
        migrations.AddConstraint(
            model_name="emailoutbox",
            constraint=models.CheckConstraint(
                condition=models.Q(("notice", ""), ("kind", "NOTIFICATION"), _connector="OR"),
                name="notifications_outbox_notice_only_notification",
            ),
        ),
        migrations.AddField(
            model_name="noticedefault",
            name="changed_by",
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.PROTECT,
                related_name="+",
                to=settings.AUTH_USER_MODEL,
                verbose_name="changed by",
            ),
        ),
        migrations.AddField(
            model_name="noticesetting",
            name="set_by",
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.PROTECT,
                related_name="+",
                to=settings.AUTH_USER_MODEL,
                verbose_name="set by",
            ),
        ),
        migrations.AddField(
            model_name="noticesetting",
            name="user",
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.CASCADE,
                related_name="notice_settings",
                to=settings.AUTH_USER_MODEL,
                verbose_name="user",
            ),
        ),
        migrations.AddConstraint(
            model_name="noticedefault",
            constraint=models.UniqueConstraint(
                fields=("notice", "channel"), name="notifications_noticedefault_one_per_channel"
            ),
        ),
        migrations.AddConstraint(
            model_name="noticesetting",
            constraint=models.UniqueConstraint(
                fields=("user", "notice", "channel"),
                name="notifications_noticesetting_one_per_user",
            ),
        ),
        migrations.AddConstraint(
            model_name="noticesetting",
            constraint=models.CheckConstraint(
                condition=models.Q(("mode__in", ["ALWAYS", "NEVER"])),
                name="notifications_noticesetting_mode",
            ),
        ),
        migrations.RunSQL(FROZEN_WITH_NOTICE, FROZEN_BEFORE),
    ]

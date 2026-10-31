"""Infrastructure tables shared by the whole project."""

from django.conf import settings
from django.db import models
from django.utils import timezone

from apps.core.ids import new_id


class BaseModel(models.Model):
    """Base of every domain model: UUIDv7 primary key and creation/update times.

    Users never see the key; documents show their ``DocumentSequence`` number instead.
    """

    id = models.UUIDField(primary_key=True, default=new_id, editable=False)
    created_at = models.DateTimeField(default=timezone.now, editable=False)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        abstract = True


class CacheEntry(models.Model):
    """A row of the security cache (``apps.core.cache.SecurityDatabaseCache``).

    The layout matches Django's ``createcachetable`` so the table is a regular cache table,
    but it is created by a migration: the one-shot ``migrate`` step owns the schema. The
    key is the cache key itself, not a UUID, because the cache addresses rows by key.
    """

    cache_key = models.CharField(max_length=255, primary_key=True)
    value = models.TextField()
    expires = models.DateTimeField(db_index=True)

    class Meta:
        db_table = "django_cache"
        verbose_name = "security cache entry"
        verbose_name_plural = "security cache entries"

    def __str__(self) -> str:
        return self.cache_key


class DocumentSequence(BaseModel):
    """Gapless counter per scope (e.g. ``PO:A:2026``); see ``apps.core.sequences``.

    The row is locked (L9) by the transaction that takes a number and stays locked until it
    ends, so a rollback returns the number and no two documents share one.
    """

    scope = models.CharField(max_length=100, unique=True)
    next_value = models.PositiveBigIntegerField(default=1)

    class Meta:
        constraints = [
            models.CheckConstraint(
                condition=models.Q(next_value__gte=1), name="core_documentsequence_next_value_gte_1"
            ),
        ]

    def __str__(self) -> str:
        return f"{self.scope} → {self.next_value}"


class IdempotencyRecord(BaseModel):
    """One executed command per (actor, key); see ``apps.core.idempotency`` (plan §4.8).

    Kept for ``IDEMPOTENCY_RECORD_RETENTION`` (7 days), much longer than a key's validity
    (24 h), so a key that is still valid always finds its record.
    """

    actor = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+", db_index=False
    )
    key_id = models.UUIDField()
    operation = models.CharField(max_length=100)
    payload_hash = models.CharField(max_length=64)
    result_type = models.CharField(max_length=100, blank=True)
    result_id = models.UUIDField(null=True, blank=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["actor", "key_id"], name="core_idempotency_actor_key"),
        ]
        indexes = [models.Index(fields=["created_at"], name="core_idempotency_created")]

    def __str__(self) -> str:
        return f"{self.operation} {self.key_id}"

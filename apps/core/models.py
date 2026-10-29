"""Infrastructure tables shared by the whole project."""

from django.db import models


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

"""The ``AccessState`` singleton (plan §3.3, §3.6)."""

from uuid import UUID

from django.db import models

ACCESS_STATE_ID = UUID(int=1)


class AccessState(models.Model):
    """One row whose ``org_generation`` moves with every scope-defining change.

    Writes lock it ``FOR SHARE`` at L0 (``authorize_in_txn``); changes to a company or
    branch ``is_active``, a branch's ``managed_by`` or a project's ``branch``,
    ``procurement_branch`` or ``status`` lock it ``FOR UPDATE``. A database trigger
    (``access.0002``) bumps the counter on each such change, whatever code path made it, so
    a revocation waits for the writes in flight and every later write sees it.
    """

    id = models.UUIDField(primary_key=True, default=ACCESS_STATE_ID, editable=False)
    org_generation = models.PositiveBigIntegerField(default=0)

    class Meta:
        constraints = [
            models.CheckConstraint(
                condition=models.Q(id=ACCESS_STATE_ID), name="access_accessstate_singleton"
            ),
        ]

    def __str__(self) -> str:
        return f"org_generation={self.org_generation}"

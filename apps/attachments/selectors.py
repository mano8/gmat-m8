"""Reading attachments, always through their parent (plan §5).

An attachment is visible exactly when its parent is, with the parent's view capability
(``parents``): a file is never reachable by someone who cannot open its document (404, the
same answer as for a document out of scope). Removed attachments are listed (who removed
them and why) but can no longer be downloaded.
"""

from dataclasses import dataclass
from typing import Any, cast
from uuid import UUID

from django.conf import settings
from django.db.models import Model
from django.urls import reverse

from apps.access.context import AccessContext
from apps.access.exceptions import AccessDenied, NotInScope
from apps.access.scopes import ScopedQuerySet
from apps.attachments.models import Attachment
from apps.attachments.parents import ParentType, parent_of, type_of
from apps.attachments.services import ADD_OPERATION
from apps.core.idempotency import issue_key


def parent(ctx: AccessContext, ptype: ParentType, parent_id: UUID) -> Model:
    """The parent document if the user can view it (``NotInScope`` otherwise)."""
    documents = cast(ScopedQuerySet[Any], ptype.model._default_manager.all())
    found = documents.visible_to(ctx, ptype.view).filter(pk=parent_id).first()
    if found is None:
        raise NotInScope(ptype.view)
    return cast(Model, found)


def attachment(ctx: AccessContext, attachment_id: UUID, *, removed: bool = False) -> Attachment:
    """An attachment whose parent the user can view; a removed one only with ``removed``.

    S7: unknown (or removed), 404; the parent's view capability held nowhere, 403; the parent
    out of its scope, 404.
    """
    found = (
        Attachment.objects.filter(pk=attachment_id)
        .select_related("request", "receipt", "handover", "uploaded_by")
        .first()
    )
    if found is None or (found.removed_at is not None and not removed):
        raise NotInScope("attachment")
    of = parent_of(found)
    view = type_of(of).view
    if not ctx.has(view):
        raise AccessDenied(view)
    if not ctx.can(view, of):
        raise NotInScope(view)
    return found


def _own_draft(ctx: AccessContext, ptype: ParentType, of: Model, item: Attachment) -> bool:
    return item.uploaded_by_id == ctx.user_id and ptype.is_draft(of) and ctx.can(ptype.add, of)


def can_remove(ctx: AccessContext, item: Attachment) -> bool:
    """The uploader on a draft, or the parent's moderators (the service checks again)."""
    of = parent_of(item)
    ptype = type_of(of)
    if item.removed_at is not None:
        return False
    return _own_draft(ctx, ptype, of, item) or ctx.can(ptype.moderate, of)


def needs_reason(ctx: AccessContext, item: Attachment) -> bool:
    of = parent_of(item)
    return not _own_draft(ctx, type_of(of), of, item)


def can_add(ctx: AccessContext, of: Model) -> bool:
    ptype = type_of(of)
    return (
        ptype.is_open(of)
        and ctx.user_id is not None
        and ptype.may_add(of, ctx.user_id)
        and ctx.can(ptype.add, of)
    )


@dataclass(frozen=True)
class Row:
    item: Attachment
    can_remove: bool
    needs_reason: bool


@dataclass(frozen=True)
class Panel:
    """What a document page shows of its attachments, and the forms it offers."""

    kind: str
    parent: Model
    back: str
    rows: list[Row]
    can_add: bool
    slots_left: int
    upload_key: str

    @property
    def upload_url(self) -> str:
        url = reverse(f"attachments:upload_{self.kind}", args=[self.parent.pk])
        return f"{url}?back={self.back}"

    @property
    def shown(self) -> bool:
        return bool(self.rows) or self.can_add


def panel(ctx: AccessContext, of: Model, *, back: str = "detail") -> Panel:
    """The attachments of a document its page already showed (read through its scope)."""
    ptype = type_of(of)
    items = list(
        Attachment.objects.filter(**{ptype.field: of}).select_related("uploaded_by", "removed_by")
    )
    rows = [Row(item, can_remove(ctx, item), needs_reason(ctx, item)) for item in items]
    slots_left = max(0, settings.ATTACHMENT_MAX_PER_PARENT - len(items))
    adding = can_add(ctx, of) and slots_left > 0
    return Panel(
        kind=ptype.kind,
        parent=of,
        back=back,
        rows=rows,
        can_add=adding,
        slots_left=slots_left,
        upload_key=issue_key(ctx.user_id, ADD_OPERATION) if adding and ctx.user_id else "",
    )

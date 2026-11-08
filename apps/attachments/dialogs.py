"""The remove dialog of an attachment (M8b.4): an inline dialog of the document page's panel.

The panel draws one per attachment the user may remove (a bounded list: at most the parent's
slots) and ``views.remove`` builds the same one around the bound form, so a refusal (a
moderator's missing reason) comes back inside the open dialog.
"""

from collections.abc import Mapping
from typing import Any
from urllib.parse import urlencode

from django.urls import reverse
from django.utils.translation import gettext as _

from apps.attachments.forms import RemoveForm
from apps.attachments.models import Attachment
from apps.core.dialogs import ActionDialog


def remove_form(
    item: Attachment, *, needs_reason: bool, data: Mapping[str, Any] | None = None
) -> RemoveForm:
    """The form of the remove dialog: a reason where one is required (a moderator's removal),
    nothing to fill in on the uploader's own draft."""
    form = RemoveForm(data, auto_id=f"id_remove_{item.pk}_%s")
    if not needs_reason:
        del form.fields["reason"]
    return form


def remove_dialog(item: Attachment, form: RemoveForm, back: str) -> ActionDialog:
    action = reverse("attachments:remove", args=[item.pk])
    return ActionDialog(
        id=f"remove-dialog-{item.pk}",
        title=_("Remove the attachment"),
        description=item.file_name,
        icon="trash",
        tone="danger",
        action=f"{action}?{urlencode({'back': back})}" if back else action,
        submit=_("Remove the attachment"),
        form=form,
        consequences=(
            _("The file can no longer be downloaded."),
            _("Its name stays on the page, marked as removed."),
        ),
        cancel=_("Keep the file"),
        testid="attachment-remove-form",
        submit_testid="attachment-remove-submit",
    )

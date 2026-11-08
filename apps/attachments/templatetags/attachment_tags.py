"""Template tags of the attachments panel (M8b.4)."""

from typing import Any

from django import template

from apps.attachments.dialogs import remove_dialog, remove_form
from apps.core.dialogs import ActionDialog

register = template.Library()


@register.simple_tag
def attachment_remove_dialog(row: Any, panel: Any) -> ActionDialog:
    """The remove dialog of a row of the panel (``Row``), as ``views.remove`` builds it."""
    form = remove_form(row.item, needs_reason=row.needs_reason)
    return remove_dialog(row.item, form, panel.back)

"""The dialogs of the order page (M8b.4): one ``ActionDialog`` per secondary action.

The page draws them and the POST-only views build the same ones around the bound form, so an
invalid or refused form comes back inside its open dialog. Ids that repeat per line, allocation
or dispatch carry its key (the line number, the allocation's or dispatch's id).
"""

from typing import Any

from django.forms import Form
from django.urls import reverse
from django.utils.translation import gettext as _

from apps.core.dialogs import ActionDialog
from apps.notifications.models import EmailOutbox
from apps.purchasing.forms import AllocationQuantityForm
from apps.purchasing.models import PurchaseAllocation, PurchaseOrder, PurchaseOrderLine


def fresh(prefix: str, key: object) -> str:
    """An ``auto_id`` that stays unique when a page holds one form per line."""
    return f"id_%s_{prefix}{key}"


def header_dialog(order: PurchaseOrder, form: Form) -> ActionDialog:
    return ActionDialog(
        id="header-dialog",
        title=_("Edit supplier, destination and notes"),
        icon="pencil",
        size="lg",
        action=reverse("purchasing:update", args=[order.pk]),
        submit=_("Save the draft"),
        form=form,
        testid="header-form",
        submit_testid="header-submit",
    )


def discard_dialog(order: PurchaseOrder, form: Form) -> ActionDialog:
    return ActionDialog(
        id="discard-dialog",
        title=_("Discard the draft"),
        icon="trash",
        tone="danger",
        action=reverse("purchasing:cancel", args=[order.pk]),
        submit=_("Discard the draft"),
        form=form,
        consequences=(
            _("The draft and its lines are thrown away."),
            _("The quantities it reserves are released."),
        ),
        cancel=_("Keep the draft"),
        testid="discard-form",
        submit_testid="discard-submit",
    )


def cancel_dialog(order: PurchaseOrder, form: Form) -> ActionDialog:
    return ActionDialog(
        id="cancel-dialog",
        title=_("Cancel the order"),
        icon="ban",
        tone="danger",
        action=reverse("purchasing:cancel", args=[order.pk]),
        submit=_("Cancel the order"),
        form=form,
        note=_("Only possible while no goods have been received."),
        consequences=(
            _("Every allocation is released."),
            _("Emails to the supplier that were not sent yet are cancelled."),
        ),
        cancel=_("Keep the order"),
        testid="cancel-form",
        submit_testid="cancel-submit",
    )


def _of(line: PurchaseOrderLine) -> str:
    """The line as the dialog names it: its number and the material (as issued, once issued)."""
    name = (line.material_snapshot or {}).get("name") or line.material.name
    return f"{line.line_no}. {name}"


def line_edit_dialog(line: PurchaseOrderLine, form: Form) -> ActionDialog:
    return ActionDialog(
        id=f"line-edit-dialog-{line.line_no}",
        title=_("Edit the line"),
        description=_of(line),
        icon="pencil",
        action=reverse("purchasing:update_line", args=[line.pk]),
        submit=_("Save the line"),
        form=form,
        testid="line-form",
        submit_testid="line-save",
    )


def line_remove_dialog(line: PurchaseOrderLine, form: Form) -> ActionDialog:
    return ActionDialog(
        id=f"line-remove-dialog-{line.line_no}",
        title=_("Remove the line"),
        description=_of(line),
        icon="trash",
        tone="danger",
        action=reverse("purchasing:remove_line", args=[line.pk]),
        submit=_("Remove the line"),
        form=form,
        consequences=(_("The line and the quantities it reserves are removed from the draft."),),
        cancel=_("Keep the line"),
        testid="line-remove-form",
        submit_testid="line-remove-submit",
    )


def line_close_dialog(line: PurchaseOrderLine, form: Form) -> ActionDialog:
    return ActionDialog(
        id=f"line-close-dialog-{line.line_no}",
        title=_("Close the line short"),
        description=_of(line),
        icon="circle-check",
        tone="danger",
        action=reverse("purchasing:close_line", args=[line.pk]),
        submit=_("Close the line short"),
        form=form,
        consequences=(
            _(
                "Give up what has not arrived (agreed with the supplier). "
                "Its allocations keep only what was received."
            ),
        ),
        cancel=_("Keep it open"),
        testid="line-close-form",
        submit_testid="line-close-submit",
    )


def raise_dialog(allocation: PurchaseAllocation, form: Form) -> ActionDialog:
    return ActionDialog(
        id=f"raise-dialog-{allocation.pk}",
        title=_("Raise the allocation"),
        icon="arrow-up",
        action=reverse("purchasing:increase_allocation", args=[allocation.pk]),
        submit=_("Raise"),
        form=form,
        testid="allocation-raise-form",
        submit_testid="allocation-raise-submit",
    )


def lower_dialog(allocation: PurchaseAllocation, form: Form) -> ActionDialog:
    return ActionDialog(
        id=f"lower-dialog-{allocation.pk}",
        title=_("Lower the allocation"),
        icon="arrow-down",
        action=reverse("purchasing:unallocate", args=[allocation.pk]),
        submit=_("Lower"),
        form=form,
        testid="allocation-lower-form",
        submit_testid="allocation-lower-submit",
    )


def resend_dialog(dispatch: EmailOutbox, form: Form) -> ActionDialog:
    return ActionDialog(
        id=f"dispatch-resend-dialog-{dispatch.pk}",
        title=_("Send again"),
        icon="send",
        action=reverse("purchasing:resend_dispatch", args=[dispatch.pk]),
        submit=_("Send again"),
        form=form,
        consequences=(_("The worker makes a new attempt to send it to the supplier."),),
        testid="dispatch-resend-form",
        submit_testid="dispatch-resend-submit",
    )


def confirm_dialog(dispatch: EmailOutbox, form: Form) -> ActionDialog:
    return ActionDialog(
        id=f"dispatch-confirm-dialog-{dispatch.pk}",
        title=_("The supplier has it"),
        icon="circle-check",
        action=reverse("purchasing:confirm_dispatch", args=[dispatch.pk]),
        submit=_("The supplier has it"),
        form=form,
        testid="dispatch-confirm-form",
        submit_testid="dispatch-confirm",
    )


def allocation_form(allocation: PurchaseAllocation, *, raising: bool, **kwargs: Any) -> Form:
    """The quantity form of an allocation's raise or lower dialog (ids unique to it)."""
    form = AllocationQuantityForm(
        auto_id=fresh("raise" if raising else "lower", allocation.pk), **kwargs
    )
    form.fields["qty"].label = _("Raise to") if raising else _("Lower to (0 removes it)")
    return form

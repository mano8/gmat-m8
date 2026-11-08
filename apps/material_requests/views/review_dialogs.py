"""The dialogs of the review page (M8b.4): one ``ActionDialog`` per secondary action.

The page draws them (``components/action_dialog.html``) and the POST-only views build the same
ones around the bound form, so an invalid or refused form comes back inside its open dialog.
A line's dialogs carry the line number in their id and in their field ids (``_form_for``), so a
page with many lines never repeats either.
"""

from django.forms import Form
from django.urls import reverse
from django.utils.translation import gettext as _

from apps.core.dialogs import ActionDialog
from apps.core.templatetags.ui_tags import quantity
from apps.material_requests.models import MaterialRequest, MaterialRequestLine

Status = MaterialRequest.Status


def line_form_ids(line: MaterialRequestLine, name: str = "") -> str:
    """Field ids unique to one form of one line of the page (``name``: which of its dialogs)."""
    return f"id_%s_{name}_{line.line_no}" if name else f"id_%s_{line.line_no}"


def request_form_ids(name: str) -> str:
    """Field ids unique to one of the request's own dialogs."""
    return f"id_{name}_%s"


def assign_dialog(found: MaterialRequest, form: Form) -> ActionDialog:
    return ActionDialog(
        id="assign-dialog",
        title=_("Send to another reviewer"),
        icon="user",
        action=reverse("requests:assign_reviewer", args=[found.pk]),
        submit=_("Send"),
        form=form,
        testid="assign-form",
        submit_testid="assign-submit",
    )


def return_dialog(found: MaterialRequest, form: Form) -> ActionDialog:
    return ActionDialog(
        id="return-dialog",
        title=_("Return for changes"),
        icon="undo-2",
        tone="danger",
        action=reverse("requests:return_to_draft", args=[found.pk]),
        submit=_("Return to the requester"),
        form=form,
        consequences=(
            _("The request goes back to a draft and every decision on its lines is cleared."),
            _("The requester reads your note, changes the request and sends it again."),
        ),
        cancel=_("Keep the request"),
        testid="return-form",
        submit_testid="return-submit",
    )


def close_dialog(found: MaterialRequest, form: Form, *, worksite_warning: bool) -> ActionDialog:
    warning = (
        _(
            "An order still on its way ships to this request's own worksite: goods may "
            "still arrive on site after the close, and will be recorded as site surplus."
        )
        if worksite_warning
        else ""
    )
    return ActionDialog(
        id="close-dialog",
        title=_("Close the request"),
        icon="circle-check",
        tone="danger",
        action=reverse("requests:close", args=[found.pk]),
        submit=_("Close request"),
        form=form,
        consequences=(
            _("Every line still open is closed short."),
            _("Reopening the request restores them."),
        ),
        warning=warning,
        warning_testid="worksite-warning",
        cancel=_("Keep it open"),
        testid="close-form",
        submit_testid="close-submit",
    )


def reopen_dialog(found: MaterialRequest, form: Form) -> ActionDialog:
    return ActionDialog(
        id="reopen-dialog",
        title=_("Reopen the request"),
        icon="rotate-ccw",
        action=reverse("requests:reopen", args=[found.pk]),
        submit=_("Reopen request"),
        form=form,
        testid="reopen-form",
        submit_testid="reopen-submit",
    )


def cancel_dialog(found: MaterialRequest, form: Form) -> ActionDialog:
    return ActionDialog(
        id="cancel-dialog",
        title=_("Cancel the request"),
        icon="ban",
        tone="danger",
        action=reverse("requests:cancel_any", args=[found.pk]),
        submit=_("Cancel request"),
        form=form,
        note=_("Only possible while nothing has been ordered or delivered."),
        cancel=_("Keep the request"),
        testid="cancel-form",
        submit_testid="cancel-submit",
    )


def reduce_dialog(line: MaterialRequestLine, form: Form, floor: object) -> ActionDialog:
    unit = line.unit.code
    return ActionDialog(
        id=f"reduce-dialog-{line.line_no}",
        title=_("Reduce the approved quantity"),
        description=f"{line.line_no}. {line.description}",
        icon="minus",
        action=reverse("requests:reduce_line", args=[line.pk]),
        submit=_("Reduce"),
        form=form,
        note=_("At least %(floor)s %(unit)s (delivered, received or on order).")
        % {"floor": quantity(floor), "unit": unit},  # type: ignore[arg-type]
        testid="reduce-form",
        submit_testid="reduce-submit",
    )


def close_line_dialog(line: MaterialRequestLine, form: Form) -> ActionDialog:
    return ActionDialog(
        id=f"close-line-dialog-{line.line_no}",
        title=_("Close the line short"),
        description=f"{line.line_no}. {line.description}",
        icon="circle-check",
        tone="danger",
        action=reverse("requests:close_line", args=[line.pk]),
        submit=_("Close short"),
        form=form,
        consequences=(_("What has not arrived is given up; the line stops waiting for goods."),),
        cancel=_("Keep it open"),
        testid="close-line-form",
        submit_testid="close-line-submit",
    )


def reopen_line_dialog(line: MaterialRequestLine, form: Form) -> ActionDialog:
    return ActionDialog(
        id=f"reopen-line-dialog-{line.line_no}",
        title=_("Reopen the line"),
        description=f"{line.line_no}. {line.description}",
        icon="rotate-ccw",
        action=reverse("requests:reopen_line", args=[line.pk]),
        submit=_("Reopen"),
        form=form,
        testid="reopen-line-form",
        submit_testid="reopen-line-submit",
    )


def remove_line_dialog(line: MaterialRequestLine, form: Form, status: str) -> ActionDialog:
    rejected = status == Status.SUBMITTED
    return ActionDialog(
        id=f"remove-line-dialog-{line.line_no}",
        title=_("Remove the line"),
        description=f"{line.line_no}. {line.description}",
        icon="trash",
        tone="danger",
        action=reverse("requests:remove_reviewed_line", args=[line.pk]),
        submit=_("Remove"),
        form=form,
        consequences=(
            _("The line stays on the request, rejected, with your reason.")
            if rejected
            else _("The line stays on the request, closed short, with your reason."),
        ),
        cancel=_("Keep the line"),
        testid="remove-line-form",
        submit_testid="remove-line-submit",
    )


def free_line_dialog(found: MaterialRequest, form: Form) -> ActionDialog:
    """A material outside the catalog, on a draft: described, with its unit and quantity."""
    return ActionDialog(
        id="free-line-dialog",
        title=_("Not in the catalog?"),
        icon="plus",
        action=reverse("requests:add_free_line", args=[found.pk]),
        submit=_("Add to the request"),
        form=form,
        testid="free-line-form",
        submit_testid="free-line-submit",
    )


def discard_draft_dialog(found: MaterialRequest, form: Form) -> ActionDialog:
    """Discarding the requester's own draft, as a confirmation dialog of its page."""
    return ActionDialog(
        id="discard-draft-dialog",
        title=_("Discard draft"),
        icon="trash",
        tone="danger",
        action=reverse("requests:cancel", args=[found.pk]),
        submit=_("Discard draft"),
        form=form,
        consequences=(
            _("The draft and its lines are thrown away."),
            _("Nothing has been sent to a reviewer yet."),
        ),
        cancel=_("Keep the draft"),
        testid="discard-draft-form",
        submit_testid="discard-draft-submit",
    )

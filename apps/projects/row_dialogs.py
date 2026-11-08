"""The dialogs of the project page (M8b.4).

A project assignment's validity and revocation are on a paged list, so they are lazy dialogs
(one shared dialog per kind of action, loaded for the row with htmx, a page of their own without
it). Sharing the project is an inline dialog of the page.
"""

from django.forms import Form
from django.urls import reverse
from django.utils.translation import gettext as _

from apps.core.dialogs import REVOKE_DIALOG, ActionDialog, validity_dialog
from apps.projects.models import Project, ProjectAssignment


def project_url(project_id: object) -> str:
    return reverse("projects:detail", args=[project_id])


def assignment_validity_dialog(assignment: ProjectAssignment, form: Form) -> ActionDialog:
    return validity_dialog(
        reverse("projects:assignment_validity", args=[assignment.pk]),
        form,
        back=project_url(assignment.project_id),
    )


def assignment_revoke_dialog(assignment: ProjectAssignment, form: Form) -> ActionDialog:
    return ActionDialog(
        id=REVOKE_DIALOG,
        title=_("Revoke access to the project"),
        description=str(assignment.user.full_name or assignment.user.email),
        icon="ban",
        tone="danger",
        action=reverse("projects:revoke_assignment", args=[assignment.pk]),
        submit=_("Revoke"),
        form=form,
        consequences=(
            _("The person stops seeing this project through this assignment, at once."),
            _("The assignment stays in the history, marked as revoked."),
        ),
        cancel=_("Keep the access"),
        back=project_url(assignment.project_id),
        testid="revoke-form",
        submit_testid="revoke-submit",
    )


def share_dialog(project: Project, form: Form) -> ActionDialog:
    return ActionDialog(
        id="share-dialog",
        title=_("Share the project"),
        icon="users",
        size="lg",
        action=reverse("projects:assign", args=[project.pk]),
        submit=_("Share"),
        form=form,
        testid="share-form",
        submit_testid="share-submit",
    )

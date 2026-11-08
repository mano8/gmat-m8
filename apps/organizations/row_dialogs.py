"""The dialogs of the person page (M8b.4).

A role's validity and revocation are on a paged list, so they are lazy dialogs (one shared
dialog per kind of action, loaded for the row with htmx, a page of their own without it).
Granting a role, offboarding and revoking an email change are inline dialogs of the page.
"""

from django.forms import Form
from django.urls import reverse
from django.utils.translation import gettext as _

from apps.accounts.models import EmailChangeRequest, User
from apps.core.dialogs import REVOKE_DIALOG, ActionDialog, validity_dialog
from apps.organizations.models import Membership


def person_url(user_id: object) -> str:
    return reverse("org:person", args=[user_id])


def membership_validity_dialog(membership: Membership, form: Form) -> ActionDialog:
    return validity_dialog(
        reverse("org:membership_validity", args=[membership.pk]),
        form,
        back=person_url(membership.user_id),
    )


def membership_revoke_dialog(membership: Membership, form: Form) -> ActionDialog:
    return ActionDialog(
        id=REVOKE_DIALOG,
        title=_("Revoke the role"),
        description=str(membership.get_role_display()),
        icon="ban",
        tone="danger",
        action=reverse("org:revoke_membership", args=[membership.pk]),
        submit=_("Revoke"),
        form=form,
        consequences=(
            _("The person loses what this role gives, at once."),
            _("The role stays in the history, marked as revoked."),
        ),
        cancel=_("Keep the role"),
        back=person_url(membership.user_id),
        testid="revoke-form",
        submit_testid="revoke-submit",
    )


def grant_dialog(person: User, form: Form) -> ActionDialog:
    return ActionDialog(
        id="grant-dialog",
        title=_("Grant a role"),
        icon="plus",
        size="lg",
        action=reverse("org:grant_membership", args=[person.pk]),
        submit=_("Grant"),
        form=form,
        testid="grant-form",
        submit_testid="grant-submit",
    )


def offboard_dialog(person: User, form: Form) -> ActionDialog:
    return ActionDialog(
        id="offboard-dialog",
        title=_("Offboard"),
        icon="log-out",
        tone="danger",
        action=reverse("org:offboard", args=[person.pk]),
        submit=_("Offboard"),
        form=form,
        consequences=(
            _("The account is deactivated and its sessions end."),
            _("Every role, project, pending invitation and email change is revoked."),
            _("That includes the invitations and email changes this person issued."),
        ),
        cancel=_("Keep the account"),
        testid="offboard-form",
        submit_testid="offboard-submit",
    )


def revoke_email_change_dialog(change: EmailChangeRequest, form: Form) -> ActionDialog:
    return ActionDialog(
        id="email-revoke-dialog",
        title=_("Revoke the email change"),
        icon="ban",
        tone="danger",
        action=reverse("org:revoke_email_change", args=[change.pk]),
        submit=_("Revoke"),
        form=form,
        consequences=(_("The address stays as it is and the pending confirmation is cancelled."),),
        cancel=_("Keep the change"),
        testid="email-revoke-form",
        submit_testid="email-revoke-submit",
    )

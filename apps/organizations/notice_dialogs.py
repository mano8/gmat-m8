"""The dialogs of the email notification screens (M15.3, Q87-Q89): one per notice on the
administration page, one on a person's page. Inline: both pages are bounded (the registry)."""

from django.forms import Form
from django.urls import reverse
from django.utils.translation import gettext as _

from apps.accounts.models import User
from apps.core.dialogs import ActionDialog
from apps.notifications.kinds import NOTICES, Notice
from apps.organizations.row_dialogs import person_url


def notice_slug(notice: Notice) -> str:
    return notice.value.lower().replace("_", "-")


def notice_default_dialog(notice: Notice, form: Form, *, turn_on: bool) -> ActionDialog:
    """Send ``notice`` to everyone again (``turn_on``) or to nobody who has no setting of their
    own. People with their own setting and people who turned business email off are never
    changed by it."""
    label = NOTICES[notice].label
    keeps = _("People who have their own setting for this email keep it.")
    refused = _("People who turned business email off never receive it.")
    if turn_on:
        title, submit = _("Turn on for everyone"), _("Turn on")
        consequences: tuple[str, ...] = (
            _("This email is sent again to everyone who receives it."),
            keeps,
            refused,
        )
    else:
        title, submit = _("Turn off for everyone"), _("Turn off")
        consequences = (
            _("This email is no longer sent to anyone who has no setting of their own."),
            keeps,
            _("A message already queued and not yet sent follows the change."),
        )
    slug = notice_slug(notice)
    return ActionDialog(
        id=f"notice-{slug}-dialog",
        title=title,
        description=str(label),
        icon="mail" if turn_on else "ban",
        action=reverse("org:notification_default", args=[notice.value]),
        submit=submit,
        form=form,
        consequences=consequences,
        testid=f"notice-{slug}-form",
        submit_testid=f"notice-{slug}-submit",
    )


def person_notices_dialog(person: User, form: Form) -> ActionDialog:
    """Choose, for one person, which emails they always or never get."""
    return ActionDialog(
        id="notices-dialog",
        title=_("Change email notifications"),
        icon="mail",
        size="lg",
        action=reverse("org:person_notifications", args=[person.pk]),
        submit=_("Save"),
        form=form,
        note=_(
            "Always and never beat the setting for everyone. Someone who turned business email"
            " off themselves receives none of them, whatever is set here. The person is not told."
        ),
        back=person_url(person.pk),
        testid="notices-form",
        submit_testid="notices-submit",
    )

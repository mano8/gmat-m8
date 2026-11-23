"""The dialogs of the notification screens of the administration (M15.3, M17.4: Q87-Q89, Q93,
Q97): one per notification on the administration page, one per channel on a person's page.
Inline: both pages are bounded (the registry)."""

from django.forms import Form
from django.urls import reverse
from django.utils.translation import gettext as _

from apps.accounts.models import User
from apps.core.dialogs import ActionDialog
from apps.notifications.kinds import CHANNELS, NOTICES, Channel, Notice, channel_slug
from apps.organizations.row_dialogs import person_url


def notice_slug(notice: Notice) -> str:
    return notice.value.lower().replace("_", "-")


def notice_default_dialog(notice: Notice, form: Form) -> ActionDialog:
    """Choose, for one notification, whether it is sent to everyone on each channel. People with
    a setting of their own, and people who chose for themselves, are never changed by it."""
    slug = notice_slug(notice)
    return ActionDialog(
        id=f"notice-{slug}-dialog",
        title=_("Change for everyone"),
        label=_("Change"),
        description=str(NOTICES[notice].label),
        icon="mail",
        action=reverse("org:notification_default", args=[notice.value]),
        submit=_("Save"),
        form=form,
        consequences=(
            _("A notification that is not sent is sent to nobody who has no setting of their own."),
            _("People who have a setting of their own, or chose for themselves, keep it."),
            _("A message already queued and not yet sent follows the change."),
        ),
        testid=f"notice-{slug}-form",
        submit_testid=f"notice-{slug}-submit",
    )


def person_notices_dialog(person: User, channel: Channel, form: Form) -> ActionDialog:
    """Choose, for one person on one channel, which notifications they always or never get."""
    slug = channel_slug(channel)
    label = CHANNELS[channel].label
    return ActionDialog(
        id=f"notices-{slug}-dialog",
        title=_("Change notifications: %(channel)s") % {"channel": label},
        label=_("Change: %(channel)s") % {"channel": label},
        icon="mail" if channel == Channel.EMAIL else "bell",
        size="lg",
        action=reverse("org:person_notifications", args=[person.pk, slug]),
        submit=_("Save"),
        form=form,
        note=_(
            "Always and never beat the setting for everyone, but the person's own choices come"
            " first: someone who chose for themselves, or turned this channel off, keeps that."
            " The person is not told."
        ),
        back=person_url(person.pk),
        testid=f"notices-{slug}-form",
        submit_testid=f"notices-{slug}-submit",
    )

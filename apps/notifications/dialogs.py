"""The dialogs of *My notifications* (M17.4, Q97): one for where the person is told (a switch per
channel), one per channel for what they are told there. Inline: the page is bounded (the
registry)."""

from django.forms import Form
from django.urls import reverse
from django.utils.translation import gettext as _

from apps.core.dialogs import ActionDialog
from apps.notifications.kinds import CHANNELS, Channel, channel_slug


def channels_dialog(form: Form) -> ActionDialog:
    """Turn the channels on or off: off, nothing of any notification reaches the person there."""
    return ActionDialog(
        id="channels-dialog",
        title=_("Change where you are told"),
        icon="bell",
        action=reverse("notifications:save_channels"),
        submit=_("Save"),
        form=form,
        note=_(
            "A channel you turn off tells you nothing, whatever you chose below; your choices are"
            " kept for when you turn it on again. Account security emails and supplier orders"
            " are always sent."
        ),
        testid="channels-form",
        submit_testid="channels-submit",
    )


def choices_dialog(channel: Channel, form: Form) -> ActionDialog:
    """Choose, for one channel, which notifications the person wants: their own choice wins over
    what the administrators set, in both directions."""
    slug = channel_slug(channel)
    label = CHANNELS[channel].label
    return ActionDialog(
        id=f"choices-{slug}-dialog",
        title=_("Change what you get: %(channel)s") % {"channel": label},
        label=_("Change: %(channel)s") % {"channel": label},
        icon="mail" if channel == Channel.EMAIL else "bell",
        size="lg",
        action=reverse("notifications:save_choices", args=[slug]),
        submit=_("Save"),
        form=form,
        note=_(
            "Your own choice wins over what the administrators set, for you only. Choose “As the"
            " administrators set it” to follow them again."
        ),
        testid=f"choices-{slug}-form",
        submit_testid=f"choices-{slug}-submit",
    )

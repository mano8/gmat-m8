"""The forms of the notification screens of the administration (M15.3, M17.4: Q87, Q89, Q93).

One form per notification for everyone (a value per channel: the value to set, never a toggle, so
a double tap sets the same value again) and one per person and channel (a choice per notification).
Both carry a server-issued idempotency key and what they showed (``seen_*``), so a save changes
only what the administrator changed; the services check everything again.
"""

from typing import Any

from django import forms
from django.utils.translation import gettext_lazy as _
from django.utils.translation import pgettext_lazy

from apps.core.forms import IdempotentForm
from apps.notifications.forms import FOLLOW, OFF, ON, NoticeChoicesForm, seen_name
from apps.notifications.kinds import CHANNELS, Channel, Mode, Notice
from apps.notifications.services import NOTICE_CHANNELS_OPERATION, PERSON_NOTICES_OPERATION

__all__ = ["FOLLOW", "NoticeDefaultForm", "PersonNoticesForm"]


class NoticeDefaultForm(IdempotentForm):
    """Send one notification to everyone or to nobody, on each channel."""

    operation = NOTICE_CHANNELS_OPERATION

    VALUES = (
        (ON, pgettext_lazy("notice state", "Sent")),
        (OFF, pgettext_lazy("notice state", "Not sent")),
    )

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        for channel, spec in CHANNELS.items():
            self.fields[channel.value] = forms.ChoiceField(label=spec.label, choices=self.VALUES)
            self.fields[seen_name(channel.value)] = forms.ChoiceField(
                required=False, choices=self.VALUES, widget=forms.HiddenInput
            )

    @staticmethod
    def initial_for(enabled: dict[Channel, bool]) -> dict[str, str]:
        """The form's initial values from the settings for everyone."""
        shown = {channel.value: ON if on else OFF for channel, on in enabled.items()}
        return shown | {seen_name(key): value for key, value in shown.items()}

    def changes(self) -> dict[Channel, bool]:
        """What the administrator changed, ready for ``set_notice_channels``: a channel whose
        value equals what the dialog showed is left out. A post without what was shown names
        every channel."""
        chosen = self.cleaned_data
        return {
            channel: chosen[channel.value] == ON
            for channel in CHANNELS
            if seen_name(channel.value) not in self.data
            or chosen[channel.value] != chosen.get(seen_name(channel.value))
        }


class PersonNoticesForm(NoticeChoicesForm):
    """A person's setting for each notification on one channel: follow the one for everyone,
    always or never."""

    operation = PERSON_NOTICES_OPERATION
    choices = (
        (FOLLOW, _("Follow the setting for everyone")),
        (Mode.ALWAYS.value, _("Always send")),
        (Mode.NEVER.value, _("Never send")),
    )

    @classmethod
    def initial_for(cls, modes: dict[Notice, Mode | None]) -> dict[str, str]:
        """The form's initial values from the person's settings (``None``: follow)."""
        return cls.initial_values(
            {notice: mode.value if mode else FOLLOW for notice, mode in modes.items()}
        )

    def modes(self) -> dict[tuple[Notice, Channel], Mode | None]:
        """What the administrator changed, ready for ``set_person_notices``."""
        return {
            (notice, self.channel): Mode(value) if value else None
            for notice, value in self.changed().items()
        }

"""The forms of the notification screens (M17.4, Q97): a person's own channels and choices, and
the table shape the administrators' person form shares.

Every form carries a server-issued idempotency key, and, next to what it shows, what it showed
(``seen_*``, hidden): a save changes only what the person changed, so a change made in another
tab since is not undone. The services check everything again.
"""

from collections.abc import Iterable, Mapping
from typing import Any

from django import forms
from django.utils.translation import gettext_lazy as _

from apps.core.forms import IdempotentForm
from apps.notifications.kinds import CHANNELS, NOTICES, Channel, Notice
from apps.notifications.services import OWN_CHANNELS_OPERATION, OWN_NOTICES_OPERATION

FOLLOW = ""  # a choice that deletes the row: follow what the level above decides
ON, OFF = "on", "off"

_CHANNEL_HELP: Mapping[Channel, Any] = {
    Channel.EMAIL: _("Business notifications by email. Account security emails are always sent."),
    Channel.IN_APP: _("Business notifications in the bell and the list, while you are signed in."),
}


def seen_name(key: str) -> str:
    return f"seen_{key}"


class OwnChannelsForm(IdempotentForm):
    """The person's switch for each channel the installation offers (Q93, level 1)."""

    operation = OWN_CHANNELS_OPERATION

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        for channel, spec in CHANNELS.items():
            self.fields[channel.value] = forms.BooleanField(
                label=spec.label, required=False, help_text=_CHANNEL_HELP.get(channel, "")
            )
            self.fields[seen_name(channel.value)] = forms.ChoiceField(
                required=False, choices=[(ON, ON), (OFF, OFF)], widget=forms.HiddenInput
            )

    @staticmethod
    def initial_for(enabled: Mapping[Channel, bool]) -> dict[str, Any]:
        shown = {channel.value: on for channel, on in enabled.items()}
        return shown | {seen_name(key): ON if on else OFF for key, on in shown.items()}

    def changes(self) -> dict[Channel, bool]:
        """What the person changed, ready for ``set_own_channels``: a channel whose box equals
        what the dialog showed is left out. A post without what was shown (no script, a script of
        one's own) names every channel."""
        chosen = self.cleaned_data
        return {
            channel: bool(chosen.get(channel.value))
            for channel in CHANNELS
            if seen_name(channel.value) not in self.data
            or (ON if chosen.get(channel.value) else OFF) != chosen.get(seen_name(channel.value))
        }


class NoticeChoicesForm(IdempotentForm):
    """One choice per notice on one channel, from a list of values (``choices``): the shape the
    person's own form and the administrators' person form share. A subclass says the operation
    and how a value reads."""

    choices: tuple[tuple[str, Any], ...] = ()

    def __init__(self, *args: Any, channel: Channel, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.channel = channel
        for notice, spec in NOTICES.items():
            self.fields[notice.value] = forms.ChoiceField(
                label=spec.label, required=False, choices=self.choices_for(notice)
            )
            self.fields[seen_name(notice.value)] = forms.ChoiceField(
                required=False, choices=self.choices, widget=forms.HiddenInput
            )

    def choices_for(self, notice: Notice) -> Iterable[tuple[str, Any]]:
        return self.choices

    @staticmethod
    def initial_values(shown: Mapping[Notice, str]) -> dict[str, str]:
        """The form's initial values from the stored ones (blank: follow)."""
        values = {notice.value: value for notice, value in shown.items()}
        return values | {seen_name(key): value for key, value in values.items()}

    def changed(self) -> dict[Notice, str]:
        """What the person changed: a notice whose choice equals what the form showed is left
        out. A post without what was shown names every notice."""
        chosen = self.cleaned_data
        return {
            notice: chosen.get(notice.value) or FOLLOW
            for notice in NOTICES
            if seen_name(notice.value) not in self.data
            or chosen.get(notice.value) != chosen.get(seen_name(notice.value))
        }


class OwnChoicesForm(NoticeChoicesForm):
    """The person's own choice for each notice on one channel: as the administrators set it, or
    wanted, or not (Q93, level 2). What following gives is written in the first option."""

    operation = OWN_NOTICES_OPERATION
    choices = (
        (FOLLOW, _("As the administrators set it")),
        (ON, _("Send it to me")),
        (OFF, _("Do not send it to me")),
    )

    def __init__(self, *args: Any, administrators: Mapping[Notice, bool], **kwargs: Any) -> None:
        self.administrators = administrators
        super().__init__(*args, **kwargs)

    def choices_for(self, notice: Notice) -> Iterable[tuple[str, Any]]:
        follow, *rest = self.choices
        gives = (
            _("As the administrators set it: sent")
            if self.administrators[notice]
            else _("As the administrators set it: not sent")
        )
        return [(follow[0], gives), *rest]

    @staticmethod
    def initial_for(choices: Mapping[Notice, bool | None]) -> dict[str, str]:
        return NoticeChoicesForm.initial_values(
            {notice: FOLLOW if on is None else ON if on else OFF for notice, on in choices.items()}
        )

    def choices_made(self) -> dict[tuple[Notice, Channel], bool | None]:
        """What the person changed, ready for ``set_own_notices`` (``None``: follow)."""
        return {
            (notice, self.channel): None if value == FOLLOW else value == ON
            for notice, value in self.changed().items()
        }

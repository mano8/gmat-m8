"""The forms of the email notification screens (M15.3, Q87, Q89).

One form per setting for everyone (the value to set, never a toggle: a double tap sets the same
value again) and one per person (a choice per notice). Both carry a server-issued idempotency
key; the services check everything again.
"""

from django import forms
from django.utils.translation import gettext_lazy as _

from apps.core.forms import IdempotentForm
from apps.notifications.kinds import NOTICES, Mode, Notice
from apps.notifications.services import NOTICE_DEFAULT_OPERATION, PERSON_NOTICES_OPERATION

FOLLOW = ""  # the person's choice that deletes their row: follow the setting for everyone


class NoticeDefaultForm(IdempotentForm):
    """Turn one notice on or off for everyone."""

    operation = NOTICE_DEFAULT_OPERATION

    enabled = forms.ChoiceField(choices=[("on", "on"), ("off", "off")], widget=forms.HiddenInput)

    def is_on(self) -> bool:
        return bool(self.cleaned_data["enabled"] == "on")


class PersonNoticesForm(IdempotentForm):
    """A person's setting for each notice: follow the one for everyone, always or never."""

    operation = PERSON_NOTICES_OPERATION

    CHOICES = (
        (FOLLOW, _("Follow the setting for everyone")),
        (Mode.ALWAYS.value, _("Always send")),
        (Mode.NEVER.value, _("Never send")),
    )

    def __init__(self, *args: object, **kwargs: object) -> None:
        super().__init__(*args, **kwargs)
        for notice, spec in NOTICES.items():
            self.fields[notice.value] = forms.ChoiceField(
                label=spec.label, required=False, choices=self.CHOICES
            )
            # What the dialog showed, posted back: a save changes only what the administrator
            # changed, so another administrator's change made since is not undone.
            self.fields[self.seen_name(notice)] = forms.ChoiceField(
                required=False, choices=self.CHOICES, widget=forms.HiddenInput
            )

    @staticmethod
    def seen_name(notice: Notice) -> str:
        return f"seen_{notice.value}"

    @classmethod
    def initial_for(cls, modes: dict[Notice, Mode | None]) -> dict[str, str]:
        """The form's initial values from the person's own settings (``None``: follow)."""
        shown = {notice.value: mode.value if mode else FOLLOW for notice, mode in modes.items()}
        return shown | {f"seen_{key}": value for key, value in shown.items()}

    def modes(self) -> dict[Notice, Mode | None]:
        """What the administrator changed, ready for ``set_person_notices``: a notice whose choice
        equals what the dialog showed is left out. A post without what was shown (no script, a
        script of one's own) names every notice."""
        chosen = self.cleaned_data
        return {
            notice: Mode(chosen[notice.value]) if chosen.get(notice.value) else None
            for notice in NOTICES
            if self.seen_name(notice) not in self.data
            or chosen.get(notice.value) != chosen.get(self.seen_name(notice))
        }

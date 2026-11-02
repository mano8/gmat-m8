from collections.abc import Iterable

from django import forms
from django.utils.translation import gettext_lazy as _


class AuditFilterForm(forms.Form):
    q = forms.CharField(
        label=_("Search"),
        required=False,
        max_length=100,
        widget=forms.TextInput(attrs={"type": "search", "enterkeyhint": "search"}),
    )
    actor = forms.CharField(label=_("Who"), required=False, max_length=100)
    object_type = forms.ChoiceField(label=_("Object"), required=False)
    since = forms.DateField(
        label=_("From"), required=False, widget=forms.DateInput(attrs={"type": "date"})
    )
    until = forms.DateField(
        label=_("To"), required=False, widget=forms.DateInput(attrs={"type": "date"})
    )

    def __init__(self, *args: object, object_types: Iterable[str] = (), **kwargs: object) -> None:
        super().__init__(*args, **kwargs)  # type: ignore[arg-type]
        self.fields["object_type"].choices = [  # type: ignore[attr-defined]
            ("", _("All")),
            *((name, name) for name in object_types),
        ]

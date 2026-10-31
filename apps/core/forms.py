"""Form pieces shared by the screens: service errors, idempotency keys, date-time inputs."""

from typing import Any

from django import forms
from django.core.exceptions import ValidationError
from django.utils.translation import gettext_lazy as _

from apps.core.idempotency import issue_key


class DateTimeLocalInput(forms.DateTimeInput):
    """The browser's own date-time picker (``<input type="datetime-local">``)."""

    input_type = "datetime-local"

    def __init__(self, attrs: dict[str, Any] | None = None) -> None:
        super().__init__(attrs=attrs, format="%Y-%m-%dT%H:%M")


def datetime_field(
    label: Any, *, required: bool = False, help_text: Any = ""
) -> forms.DateTimeField:
    return forms.DateTimeField(
        label=label,
        required=required,
        help_text=help_text,
        widget=DateTimeLocalInput(),
        input_formats=["%Y-%m-%dT%H:%M", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M"],
    )


class IdempotentForm(forms.Form):
    """A create form: carries a server-issued idempotency key (plan §4.8)."""

    idempotency_key = forms.CharField(widget=forms.HiddenInput)

    operation: str = ""

    def __init__(self, *args: Any, actor_id: Any = None, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        if not self.is_bound and actor_id is not None:
            self.initial["idempotency_key"] = issue_key(actor_id, self.operation)


class ValidityForm(forms.Form):
    """A membership's or assignment's validity window (S8: half-open, UTC at rest)."""

    valid_from = datetime_field(_("Valid from"), required=True)
    valid_until = datetime_field(_("Valid until"), help_text=_("Leave empty for no end."))

    def clean(self) -> dict[str, Any]:
        data = super().clean() or {}
        start, end = data.get("valid_from"), data.get("valid_until")
        if start and end and end <= start:
            self.add_error("valid_until", _("The end must be after the start."))
        return data


def add_service_errors(form: forms.Form, exc: ValidationError) -> None:
    """Show a service's ``ValidationError`` on the form: per field where it has one."""
    if hasattr(exc, "error_dict"):
        for field, errors in exc.error_dict.items():
            form.add_error(field if field in form.fields else None, errors)
    else:
        form.add_error(None, exc)


def error_text(exc: ValidationError) -> str:
    """One line for a flash message."""
    return " ".join(str(message) for message in exc.messages)


def form_error_text(form: forms.Form) -> str:
    """A form's errors in one line, for a flash message after a redirect."""
    return " ".join(str(error) for errors in form.errors.values() for error in errors)

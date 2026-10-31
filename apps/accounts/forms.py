from typing import Any

from allauth.account.adapter import get_adapter
from allauth.account.forms import ResetPasswordKeyForm as AllauthResetPasswordKeyForm
from django import forms
from django.contrib.auth import password_validation
from django.core.exceptions import ValidationError
from django.utils.translation import gettext_lazy as _

from apps.accounts.models import Invitation, User


class ResetPasswordKeyForm(AllauthResetPasswordKeyForm):
    """Sets the new password through the locked redemption (``AccountAdapter``).

    ``save`` raises ``PasswordResetTokenInvalid`` when a concurrent redemption won.
    """

    def save(self) -> None:
        get_adapter().redeem_password_reset(  # type: ignore[attr-defined]
            self.user, self.temp_key, self.cleaned_data["password1"]
        )


class AcceptInvitationForm(forms.Form):
    """The password of a new or returning account (Django's validators, minimum 12)."""

    password1 = forms.CharField(
        label=_("Password"),
        strip=False,
        widget=forms.PasswordInput(attrs={"autocomplete": "new-password"}),
    )
    password2 = forms.CharField(
        label=_("Password (again)"),
        strip=False,
        widget=forms.PasswordInput(attrs={"autocomplete": "new-password"}),
    )

    def __init__(self, *args: Any, invitation: Invitation, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.invitation = invitation

    def clean(self) -> dict[str, Any]:
        data = super().clean() or {}
        first, second = data.get("password1"), data.get("password2")
        if first and second and first != second:
            self.add_error("password2", _("The two passwords differ."))
        elif first:
            subject = self.invitation.target_user or User(
                email=self.invitation.email, full_name=self.invitation.full_name
            )
            try:
                password_validation.validate_password(first, subject)
            except ValidationError as exc:
                self.add_error("password1", exc)
        return data

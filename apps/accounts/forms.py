from allauth.account.adapter import get_adapter
from allauth.account.forms import ResetPasswordKeyForm as AllauthResetPasswordKeyForm


class ResetPasswordKeyForm(AllauthResetPasswordKeyForm):
    """Sets the new password through the locked redemption (``AccountAdapter``).

    ``save`` raises ``PasswordResetTokenInvalid`` when a concurrent redemption won.
    """

    def save(self) -> None:
        get_adapter().redeem_password_reset(  # type: ignore[attr-defined]
            self.user, self.temp_key, self.cleaned_data["password1"]
        )

from typing import Any

from allauth.account.views import PasswordResetFromKeyView as AllauthPasswordResetFromKeyView
from django.http import HttpResponse
from django.utils.decorators import method_decorator
from django.views.decorators.debug import sensitive_post_parameters

from apps.accounts.adapters import PasswordResetTokenInvalid
from apps.accounts.forms import ResetPasswordKeyForm


@method_decorator(sensitive_post_parameters("password1", "password2"), name="dispatch")
class PasswordResetFromKeyView(AllauthPasswordResetFromKeyView):
    """allauth's reset view with the token re-validated under the user-row lock.

    The key never stays in the address bar: allauth stores it in the session and redirects
    to a key-less URL. A redemption that loses the race shows the invalid-link page.
    """

    form_class = ResetPasswordKeyForm

    def get_form_class(self) -> Any:
        return ResetPasswordKeyForm

    def form_valid(self, form: Any) -> HttpResponse:
        try:
            return super().form_valid(form)
        except PasswordResetTokenInvalid:
            response: HttpResponse = self.render_to_response(self.get_context_data(token_fail=True))
            return response


password_reset_from_key = PasswordResetFromKeyView.as_view()

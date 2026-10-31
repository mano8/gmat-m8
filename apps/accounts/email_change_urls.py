"""``/account/email-change/``: the email-change links and their token-less pages (plan §5).

One link pattern serves both tokens: the new address's confirmation and the old address's
cancellation. The Caddyfile and ``config.logging`` redact the path after the prefix.
"""

from django.urls import path, re_path

from apps.accounts import views
from apps.accounts.tokens import TOKEN_PATTERN

app_name = "email_change"

urlpatterns = [
    path("confirm/", views.email_change_confirm, name="confirm"),
    path("confirm/submit/", views.email_change_confirm_submit, name="confirm_submit"),
    path("cancel/", views.email_change_cancel, name="cancel"),
    path("cancel/submit/", views.email_change_cancel_submit, name="cancel_submit"),
    re_path(rf"^(?P<token>{TOKEN_PATTERN})/$", views.email_change_link, name="link"),
]

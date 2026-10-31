"""``/invite/``: the invitation link and its token-less acceptance page (plan §5).

The Caddyfile and ``config.logging`` redact the path after ``/invite/``.
"""

from django.urls import path, re_path

from apps.accounts import views
from apps.accounts.tokens import TOKEN_PATTERN

app_name = "invitations"

urlpatterns = [
    path("accept/", views.invitation_accept, name="accept"),
    path("accept/submit/", views.invitation_submit, name="submit"),
    re_path(rf"^(?P<token>{TOKEN_PATTERN})/$", views.invitation_link, name="link"),
]

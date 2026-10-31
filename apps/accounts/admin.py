"""Read-only admin for accounts (plan §5, "Django Admin").

Users, invitations and email changes change through the in-app screens and their services
(M2.6), never in the admin; token hashes are never displayed.
The third-party registrations (Django's groups, allauth's email addresses and
authenticators) are replaced: groups are unused, and the others are read-only, with MFA
secrets never displayed.
"""

from allauth.account.models import EmailAddress
from allauth.mfa.models import Authenticator
from django.contrib import admin
from django.contrib.auth.models import Group

from apps.accounts.models import EmailChangeRequest, Invitation, User
from apps.core.admin import ReadOnlyModelAdmin

for model in (Group, EmailAddress, Authenticator):
    if admin.site.is_registered(model):
        admin.site.unregister(model)


@admin.register(User)
class UserAdmin(ReadOnlyModelAdmin):
    list_display = ("email", "full_name", "is_active", "is_staff", "offboarded_at")
    list_filter = ("is_active", "is_staff")
    search_fields = ("email", "full_name")
    ordering = ("email",)
    exclude = ("password", "user_permissions", "groups")


@admin.register(EmailAddress)
class EmailAddressAdmin(ReadOnlyModelAdmin):
    list_display = ("email", "user", "primary", "verified")
    search_fields = ("email",)


@admin.register(Authenticator)
class AuthenticatorAdmin(ReadOnlyModelAdmin):
    list_display = ("user", "type", "created_at", "last_used_at")
    list_filter = ("type",)
    exclude = ("data",)


@admin.register(Invitation)
class InvitationAdmin(ReadOnlyModelAdmin):
    list_display = (
        "email",
        "kind",
        "invited_by",
        "created_at",
        "expires_at",
        "accepted_at",
        "revoked_at",
    )
    list_filter = ("kind", "bootstrap")
    list_select_related = ("invited_by",)
    search_fields = ("email",)
    exclude = ("token_hash",)


@admin.register(EmailChangeRequest)
class EmailChangeRequestAdmin(ReadOnlyModelAdmin):
    list_display = ("user", "new_email", "requested_by", "created_at", "confirmed_at", "applied_at")
    list_select_related = ("user", "requested_by")
    search_fields = ("user__email", "new_email")
    exclude = ("token_hash", "cancel_token_hash")

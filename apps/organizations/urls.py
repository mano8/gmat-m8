from django.urls import path

from apps.organizations import views

app_name = "org"

urlpatterns = [
    path("people/", views.people, name="people"),
    path("people/<uuid:user_id>/", views.person, name="person"),
    path(
        "people/<uuid:user_id>/memberships/grant/", views.grant_membership, name="grant_membership"
    ),
    path("people/<uuid:user_id>/employer/", views.set_employer, name="set_employer"),
    path("people/<uuid:user_id>/offboard/", views.offboard, name="offboard"),
    path("people/<uuid:user_id>/email/", views.request_email_change, name="request_email_change"),
    path(
        "memberships/<uuid:membership_id>/revoke/",
        views.revoke_membership,
        name="revoke_membership",
    ),
    path(
        "memberships/<uuid:membership_id>/validity/",
        views.membership_validity,
        name="membership_validity",
    ),
    path(
        "email-changes/<uuid:change_id>/revoke/",
        views.revoke_email_change,
        name="revoke_email_change",
    ),
    path("invitations/", views.invitations, name="invitations"),
    path("invitations/new/", views.new_invitation, name="new_invitation"),
    path("invitations/create/", views.create_invitation, name="create_invitation"),
    path(
        "invitations/<uuid:invitation_id>/resend/",
        views.resend_invitation,
        name="resend_invitation",
    ),
    path(
        "invitations/<uuid:invitation_id>/revoke/",
        views.revoke_invitation,
        name="revoke_invitation",
    ),
]

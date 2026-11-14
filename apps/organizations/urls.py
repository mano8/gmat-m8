from django.urls import include, path

from apps.organizations import views

app_name = "org"

structure = [
    path("companies/", views.companies, name="companies"),
    path("companies/new/", views.new_company, name="new_company"),
    path("companies/create/", views.create_company, name="create_company"),
    path("companies/<uuid:company_id>/", views.company, name="company"),
    path("companies/<uuid:company_id>/update/", views.update_company, name="update_company"),
    path(
        "companies/<uuid:company_id>/deactivate/confirm/",
        views.confirm_deactivate_company,
        name="confirm_deactivate_company",
    ),
    path(
        "companies/<uuid:company_id>/deactivate/",
        views.deactivate_company,
        name="deactivate_company",
    ),
    path(
        "companies/<uuid:company_id>/reactivate/confirm/",
        views.confirm_reactivate_company,
        name="confirm_reactivate_company",
    ),
    path(
        "companies/<uuid:company_id>/reactivate/",
        views.reactivate_company,
        name="reactivate_company",
    ),
    path(
        "companies/<uuid:company_id>/members/add/",
        views.add_venture_member,
        name="add_venture_member",
    ),
    path(
        "venture-members/<uuid:member_id>/edit/",
        views.edit_venture_member,
        name="edit_venture_member",
    ),
    path(
        "venture-members/<uuid:member_id>/update/",
        views.update_venture_member,
        name="update_venture_member",
    ),
    path("branches/", views.branches, name="branches"),
    path("branches/new/", views.new_branch, name="new_branch"),
    path("branches/create/", views.create_branch, name="create_branch"),
    path("branches/<uuid:branch_id>/", views.branch, name="branch"),
    path("branches/<uuid:branch_id>/update/", views.update_branch, name="update_branch"),
    path(
        "branches/<uuid:branch_id>/manager/preview/",
        views.preview_branch_manager,
        name="preview_branch_manager",
    ),
    path(
        "branches/<uuid:branch_id>/manager/",
        views.set_branch_manager,
        name="set_branch_manager",
    ),
    path(
        "branches/<uuid:branch_id>/self-approval/",
        views.set_self_approval,
        name="set_self_approval",
    ),
    path(
        "branches/<uuid:branch_id>/deactivate/confirm/",
        views.confirm_deactivate_branch,
        name="confirm_deactivate_branch",
    ),
    path(
        "branches/<uuid:branch_id>/deactivate/",
        views.deactivate_branch,
        name="deactivate_branch",
    ),
    path(
        "branches/<uuid:branch_id>/reactivate/confirm/",
        views.confirm_reactivate_branch,
        name="confirm_reactivate_branch",
    ),
    path(
        "branches/<uuid:branch_id>/reactivate/",
        views.reactivate_branch,
        name="reactivate_branch",
    ),
]

urlpatterns = [
    path("people/", views.people, name="people"),
    path("people/<uuid:user_id>/", views.person, name="person"),
    path(
        "people/<uuid:user_id>/memberships/grant/", views.grant_membership, name="grant_membership"
    ),
    path("people/<uuid:user_id>/employer/", views.set_employer, name="set_employer"),
    path("people/<uuid:user_id>/offboard/", views.offboard, name="offboard"),
    path("people/<uuid:user_id>/email/", views.request_email_change, name="request_email_change"),
    # M10.4: inline dialogs of the person's page (inventory §3.4).
    path("people/<uuid:user_id>/name/", views.rename_person, name="rename_person"),
    path("people/<uuid:user_id>/mfa-reset/", views.reset_mfa, name="reset_mfa"),
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
        "memberships/<uuid:membership_id>/validity/edit/",
        views.membership_validity_form,
        name="edit_membership_validity",
    ),
    path(
        "memberships/<uuid:membership_id>/revoke/confirm/",
        views.membership_revoke_form,
        name="confirm_revoke_membership",
    ),
    path(
        "email-changes/<uuid:change_id>/revoke/",
        views.revoke_email_change,
        name="revoke_email_change",
    ),
    path(
        "email-changes/<uuid:change_id>/delivery/",
        views.email_change_delivery,
        name="email_change_delivery",
    ),
    path("invitations/", views.invitations, name="invitations"),
    path("invitations/new/", views.new_invitation, name="new_invitation"),
    path("invitations/create/", views.create_invitation, name="create_invitation"),
    path(
        "invitations/<uuid:invitation_id>/delivery/",
        views.invitation_delivery,
        name="invitation_delivery",
    ),
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
    # M10.2: companies, joint ventures and branches (inventory §3.1, §3.2).
    path("structure/", include(structure)),
]

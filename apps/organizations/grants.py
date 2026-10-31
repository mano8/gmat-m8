"""Rules for granting a membership, shared by the membership services and invitation acceptance.

They run inside a transaction that has already been authorized (``grant_membership``'s
session principal, or the invitation's token principal) and has locked the user's row.
"""

from uuid import UUID

from django.core.exceptions import ValidationError
from django.db.models import F
from django.db.models.functions import Coalesce, Greatest, Now
from django.utils.translation import gettext_lazy as _

from apps.access.roles import COMPANY_ROLES
from apps.accounts.models import User
from apps.organizations.models import Branch, Company, Membership


def check_membership_target(membership: Membership, employer_company_id: UUID | None) -> None:
    """The granted branch or company is active; a partner's company is their employer."""
    if membership.branch_id is not None:
        branch = Branch.objects.select_related("company").get(pk=membership.branch_id)
        if not (branch.is_active and branch.company.is_active):
            raise ValidationError(
                {"branch": ValidationError(_("The branch is inactive."), code="inactive")}
            )
    if membership.company_id is not None:
        if not Company.objects.filter(pk=membership.company_id, is_active=True).exists():
            raise ValidationError(
                {"company": ValidationError(_("The company is inactive."), code="inactive")}
            )
        if membership.role in COMPANY_ROLES and membership.company_id != employer_company_id:
            raise ValidationError(
                {
                    "company": ValidationError(
                        _("A partner's membership is on the company that employs them."),
                        code="not_employer",
                    )
                }
            )


def elevate_privilege(user_id: UUID) -> None:
    """Granting a privileged role moves ``privilege_elevated_at`` to now (plan §3.6).

    The database clock of the grant; never moved back. Sessions must complete MFA after it
    before they use any privileged grant, including one that starts later: an elevation in
    the future would refuse every MFA done before then, even a step-up, and lock an
    already privileged user out until that time.
    """
    User.objects.filter(pk=user_id).update(
        privilege_elevated_at=Greatest(Coalesce(F("privilege_elevated_at"), Now()), Now())
    )

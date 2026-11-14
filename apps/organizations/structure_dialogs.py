"""The dialogs of the company and branch screens (M10.2, the M8b.4 contract).

Inline on the bounded company and branch pages: editing the data, adding a joint-venture
member, the managing branch and self-approval. Lazy (a GET page of their own, loaded into the
page's shared dialog with htmx): a new company or branch from a list, a member's share from
its paged row, and the activity changes, whose preview is read only when the dialog opens.
A scope-defining change is a danger dialog that shows its preview (``body``) and carries the
generation that preview was read at; the service decides.
"""

from collections.abc import Callable
from dataclasses import dataclass

from django.forms import Form
from django.urls import reverse
from django.utils.translation import gettext as _

from apps.core.dialogs import ActionDialog
from apps.organizations.models import Branch, Company, JointVentureMember
from apps.organizations.selectors import ScopePreview

PREVIEW = "organizations/scope_preview.html"
MANAGER_BODY = "organizations/manager_preview.html"

NEW_DIALOG = "new-dialog"
ACTIVITY_DIALOG = "activity-dialog"
SHARE_DIALOG = "share-dialog"

type Preview = ScopePreview | Callable[[], ScopePreview | None] | None


def company_url(company_id: object) -> str:
    return reverse("org:company", args=[company_id])


def branch_url(branch_id: object) -> str:
    return reverse("org:branch", args=[branch_id])


# --- companies ---------------------------------------------------------------------------------


def new_company_dialog(form: Form) -> ActionDialog:
    return ActionDialog(
        id=NEW_DIALOG,
        title=_("New company"),
        icon="plus",
        action=reverse("org:create_company"),
        submit=_("Create"),
        form=form,
        note=_("Its head-office branch is created with it."),
        back=reverse("org:companies"),
        testid="company-new-form",
        submit_testid="company-new-submit",
    )


def company_edit_dialog(company: Company, form: Form) -> ActionDialog:
    return ActionDialog(
        id="company-edit-dialog",
        title=_("Edit the company"),
        description=company.code,
        icon="pencil",
        action=reverse("org:update_company", args=[company.pk]),
        submit=_("Save"),
        form=form,
        testid="company-edit-form",
        submit_testid="company-edit-submit",
    )


@dataclass(frozen=True)
class _Wording:
    """What an activity dialog says: deactivating or reactivating."""

    title: str
    submit: str
    cancel: str
    consequences: tuple[str, ...]
    warning: str = ""


@dataclass(frozen=True)
class _Activity:
    """Which object, which way, and where its dialog posts and goes back to."""

    of: Company | Branch
    active: bool
    action: str
    back: str


def _activity(what: _Activity, spec: _Wording, form: Form, preview: Preview) -> ActionDialog:
    return ActionDialog(
        id=ACTIVITY_DIALOG,
        title=spec.title,
        description=str(what.of),
        icon="rotate-ccw" if what.active else "ban",
        tone="" if what.active else "danger",
        action=what.action,
        submit=spec.submit,
        form=form,
        consequences=spec.consequences,
        warning=spec.warning,
        warning_testid="activity-warning",
        cancel=spec.cancel,
        back=what.back,
        body=PREVIEW,
        data=preview,
        testid="activity-form",
        submit_testid="activity-submit",
    )


def _company_wording(active: bool) -> _Wording:
    if active:
        return _Wording(
            _("Reactivate the company"),
            _("Reactivate"),
            _("Keep the company inactive"),
            (
                _("Its active branches and its partners' roles grant access again."),
                _("New documents can be made for its branches again."),
            ),
        )
    return _Wording(
        _("Deactivate the company"),
        _("Deactivate"),
        _("Keep the company active"),
        (
            _("Its branches and its partners' roles grant nothing while it is inactive."),
            _("No new document is made for its branches; managing branches lose them."),
            _("Nothing is deleted: it can be reactivated, and its history stays."),
        ),
    )


def company_activity_dialog(
    company: Company, form: Form, *, active: bool, preview: Preview
) -> ActionDialog:
    """Deactivate or reactivate a company, with what it does to access."""
    name = "org:reactivate_company" if active else "org:deactivate_company"
    what = _Activity(company, active, reverse(name, args=[company.pk]), company_url(company.pk))
    return _activity(what, _company_wording(active), form, preview)


def venture_member_dialog(venture: Company, form: Form) -> ActionDialog:
    return ActionDialog(
        id="member-add-dialog",
        title=_("Add a member company"),
        icon="plus",
        action=reverse("org:add_venture_member", args=[venture.pk]),
        submit=_("Add"),
        form=form,
        note=_("Members are informational: they change nobody's access."),
        testid="member-add-form",
        submit_testid="member-add-submit",
    )


def venture_share_dialog(member: JointVentureMember, form: Form) -> ActionDialog:
    return ActionDialog(
        id=SHARE_DIALOG,
        title=_("Change the share"),
        description=str(member.member),
        action=reverse("org:update_venture_member", args=[member.pk]),
        submit=_("Save"),
        form=form,
        back=company_url(member.venture_id),
        testid="share-form",
        submit_testid="share-submit",
    )


# --- branches ----------------------------------------------------------------------------------


def new_branch_dialog(form: Form, *, back: str = "") -> ActionDialog:
    return ActionDialog(
        id=NEW_DIALOG,
        title=_("New branch"),
        icon="plus",
        size="lg",
        action=reverse("org:create_branch"),
        submit=_("Create"),
        form=form,
        note=_("Its warehouses are added after it."),
        back=back or reverse("org:branches"),
        testid="branch-new-form",
        submit_testid="branch-new-submit",
    )


def branch_edit_dialog(branch: Branch, form: Form) -> ActionDialog:
    return ActionDialog(
        id="branch-edit-dialog",
        title=_("Edit the branch"),
        description=str(branch),
        icon="pencil",
        action=reverse("org:update_branch", args=[branch.pk]),
        submit=_("Save"),
        form=form,
        testid="branch-edit-form",
        submit_testid="branch-edit-submit",
    )


def branch_manager_dialog(branch: Branch, form: Form, *, preview: Preview) -> ActionDialog:
    """Choose the managing branch; choosing one shows what changes (``MANAGER_BODY``)."""
    return ActionDialog(
        id="manager-dialog",
        title=_("Change the managing branch"),
        description=str(branch),
        icon="git-branch",
        tone="danger",
        action=reverse("org:set_branch_manager", args=[branch.pk]),
        submit=_("Change"),
        form=form,
        consequences=(
            _("Who works for the managing branch reaches this branch, its projects and documents."),
            _("Who works for the branch that managed it until now no longer does."),
        ),
        cancel=_("Keep it as it is"),
        body=MANAGER_BODY,
        data=preview,
        testid="manager-form",
        submit_testid="manager-submit",
    )


def self_approval_dialog(branch: Branch, form: Form, *, allow: bool) -> ActionDialog:
    """Turn self-approval on or off, naming the self-decision rule (ADR-0014, ADR-0019)."""
    rule = _(
        "Group administrators, material managers and engineers decide their own requests"
        " either way, through their role on the request and with a second factor."
    )
    if allow:
        title, submit = _("Allow self-approval"), _("Allow")
        consequences: tuple[str, ...] = (
            _(
                "Whoever reviews requests bought by this branch may then approve, reject, return"
                " or reduce their own requests too."
            ),
            rule,
        )
    else:
        title, submit = _("Stop self-approval"), _("Stop")
        consequences = (
            _("A reviewer's own requests bought by this branch then go to another reviewer."),
            rule,
        )
    return ActionDialog(
        id="self-approval-dialog",
        title=title,
        description=str(branch),
        icon="user-check",
        action=reverse("org:set_self_approval", args=[branch.pk]),
        submit=submit,
        form=form,
        consequences=consequences,
        testid="self-approval-form",
        submit_testid="self-approval-submit",
    )


def _branch_wording(branch: Branch, active: bool) -> _Wording:
    if active:
        stays_off = _(
            "Its company is inactive: the branch grants nothing until the company is reactivated."
        )
        return _Wording(
            _("Reactivate the branch"),
            _("Reactivate"),
            _("Keep the branch inactive"),
            (
                _("Its roles grant access again, and the branch that manages it reaches it."),
                _("Its projects accept new documents again."),
            ),
            warning="" if branch.company.is_active else stays_off,
        )
    return _Wording(
        _("Deactivate the branch"),
        _("Deactivate"),
        _("Keep the branch active"),
        (
            _("Its roles grant nothing while it is inactive."),
            _("The branch that manages it no longer reaches it, and it reaches none."),
            _("Its projects accept no new documents. Nothing is deleted."),
        ),
    )


def branch_activity_dialog(
    branch: Branch, form: Form, *, active: bool, preview: Preview
) -> ActionDialog:
    """Deactivate or reactivate a branch, with what it does to access."""
    name = "org:reactivate_branch" if active else "org:deactivate_branch"
    what = _Activity(branch, active, reverse(name, args=[branch.pk]), branch_url(branch.pk))
    return _activity(what, _branch_wording(branch, active), form, preview)

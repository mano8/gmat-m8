"""The access acceptance matrix (plan §8.2): every URL and method, as every actor.

A row is keyed by URL name (``namespace:name``; the route inside its namespace for an
unnamed pattern) and lists what ``GET``, ``HEAD`` and ``POST`` return to each actor of
``tests.access.matrix.ACTORS``: every role, the mixed-membership user (S6), and the revoked
cases (expired membership, revoked membership and assignment, inactive branch, a partner
of an inactive company, a deactivated user, a session whose password changed). Every
other method is a 405 on every URL. The test fails on a URL without a row and on a row
without a URL.

Each row also lists the sentinels (plan §8.1) its responses may contain; every other
sentinel is checked absent from every response. A URL with an object adds one row per
object position (``Route.position``).

The account pages (allauth), the admin, the health checks, the token pages (invitations,
email changes), the screens of M2.6 (projects, people, invitations), the master-data
screens of M3 (catalog, suppliers) and the requester's screens of M4.3 have rows; later
business screens add theirs with the steps that build them.
"""

import re
from collections.abc import Callable
from datetime import timedelta
from typing import Any

import pytest
from allauth.mfa.models import Authenticator
from django.conf import settings
from django.contrib import admin
from django.contrib.auth.hashers import make_password
from django.contrib.contenttypes.models import ContentType
from django.urls import reverse
from django.utils import timezone

from apps.access.admin import CapabilityModelAdmin
from apps.access.models import AccessState
from apps.accounts.models import User
from apps.accounts.sessions import bump_session_epoch
from apps.audit.models import AuditEvent
from apps.core.admin import ReadOnlyModelAdmin, ServiceBackedModelAdmin
from apps.organizations.models import Company, Membership
from tests.access.matrix import (
    ACTOR_NAMES,
    ADMIN_LOGIN,
    DENIED,
    HOME,
    LOGIN,
    NOT_ALLOWED,
    NOT_FOUND,
    OK,
    OTHER_METHODS,
    REAUTHENTICATE,
    REDIRECT,
    STEP_UP,
    TABLE_METHODS,
    Expect,
    Outcome,
    Route,
    To,
    World,
    check_route,
    expect,
    missing_rows,
    send,
    url_patterns,
)
from tests.org import Sentinel

pytestmark = pytest.mark.django_db

# --- row helpers -------------------------------------------------------------------------


def same(outcomes: Expect, post: Expect | None = None) -> dict[str, Expect]:
    """``GET`` and ``HEAD`` answer alike; ``POST`` as ``post`` (default: the same)."""
    return {"GET": outcomes, "HEAD": outcomes, "POST": outcomes if post is None else post}


def public(get: Outcome = OK, post: Outcome = NOT_ALLOWED) -> dict[str, Expect]:
    """Answers everybody alike: health checks, password reset, pre-login pages."""
    return same(expect(get), expect(post))


def signed_in(get: Outcome, post: Outcome | None = None) -> dict[str, Expect]:
    """An account page: login first; reachable without session MFA (allauth's own pages)."""
    return same(expect(get, signed_out=LOGIN), expect(post or get, signed_out=LOGIN))


def admin_page(
    get: Outcome,
    post: Outcome,
    *,
    operator_get: Outcome | None = None,
    operator_post: Outcome | None = None,
) -> dict[str, Expect]:
    """An admin URL: session MFA first (step-up, 403 on POST), then the site's admission.

    Signed-out and not-admitted sessions are sent to the admin login, which refuses them.
    ``get`` / ``post`` are what the admitted users receive.
    """
    gate = {"signed_out": ADMIN_LOGIN, "mfa_not_admitted": ADMIN_LOGIN}
    get_row = expect(STEP_UP, **gate, group_admin=get, operator=operator_get or get)
    post_row = expect(DENIED, **gate, group_admin=post, operator=operator_post or post)
    return {"GET": get_row, "HEAD": get_row, "POST": post_row}


# --- admin objects -----------------------------------------------------------------------

ADMIN_MODEL_URL = re.compile(
    r"^admin:(?P<app>[a-z_]+?)_(?P<model>[a-z]+)_(?P<view>changelist|add|history|delete|change)$"
)


def admin_object(world: World, model: str) -> Any:
    """The row each admin model's object URLs open (``tests/access/conftest.py``)."""
    people, org = world.people, world.org
    rows: dict[str, Callable[[], Any]] = {
        "user": lambda: people.tech_mad,
        "authenticator": lambda: Authenticator.objects.get(user=people.tech_mad),
        "accessstate": lambda: AccessState.objects.get(),
        "company": lambda: org.a,
        "branch": lambda: org.a_mad,
        "project": lambda: org.hospital,
        "location": lambda: org.warehouses["A Madrid"],
        "supplier": lambda: org.supplier,
    }
    return rows.get(model, lambda: world.objects[model])()


def admin_model_path(key: str) -> Callable[[World], str]:
    match = ADMIN_MODEL_URL.fullmatch(key)
    assert match is not None, key
    if match["view"] in {"changelist", "add"}:
        return lambda world: reverse(key)
    return lambda world: reverse(key, args=[admin_object(world, match["model"]).pk])


def admin_redirect_path(app: str, model: str) -> Callable[[World], str]:
    def path(world: World) -> str:
        pk = admin_object(world, model).pk
        return f"/{settings.ADMIN_URL_PATH}/{app}/{model}/{pk}/"

    return path


SUPPLIER_SENTINELS = frozenset(
    {Sentinel.SUPPLIER_NAME, Sentinel.SUPPLIER_EMAIL, Sentinel.SUPPLIER_PHONE}
)

# Access and document models are read-only; master data is editable by GROUP_ADMIN only
# (the platform operator only views it). Nothing is ever deleted (plan §5, §8.2).
READ_ONLY_MODELS = {
    "accounts": ("user", "invitation", "emailchangerequest"),
    "account": ("emailaddress",),
    "mfa": ("authenticator",),
    "audit": ("auditevent",),
    "access": ("accessstate",),
    "organizations": ("membership",),
    "projects": ("project", "projectassignment"),
    "catalog": ("unitofmeasure",),
    "material_requests": ("materialrequest", "materialrequestline"),
}
MASTER_DATA_MODELS = {
    "organizations": ("company", "jointventuremember", "branch"),
    "locations": ("location",),
    "catalog": ("materialcategory", "material"),
    "suppliers": ("supplier",),
}
# The admin pages of a model that shows its own sentinels (the supplier's contact details).
ADMIN_SENTINELS = {"supplier": SUPPLIER_SENTINELS}


def _admin_model_routes() -> dict[str, Route]:
    routes: dict[str, Route] = {}
    for editable, models in ((False, READ_ONLY_MODELS), (True, MASTER_DATA_MODELS)):
        for app, names in models.items():
            for model in names:
                base = f"admin:{app}_{model}"
                if editable:
                    # An empty form answers 200 with errors: access granted, nothing saved.
                    # A joint-venture member's only editable field is optional, so its
                    # empty change is valid and saved through the service.
                    change_post = REDIRECT if model == "jointventuremember" else OK
                    add = admin_page(OK, OK, operator_get=DENIED, operator_post=DENIED)
                    change = admin_page(OK, change_post, operator_post=DENIED)
                else:
                    add = admin_page(DENIED, DENIED)
                    change = admin_page(OK, DENIED)
                views = {
                    "changelist": admin_page(OK, OK),
                    "add": add,
                    "history": admin_page(OK, OK),
                    "delete": admin_page(DENIED, DENIED),
                    "change": change,
                }
                shown = ADMIN_SENTINELS.get(model, frozenset())
                for view, methods in views.items():
                    key = f"{base}_{view}"
                    routes[key] = Route(methods, path=admin_model_path(key), allowed=shown)
                routes[f"admin:{app}/{model}/<path:object_id>/"] = Route(
                    admin_page(REDIRECT, REDIRECT),
                    path=admin_redirect_path(app, model),
                    allowed=shown,
                )
    return routes


ADMIN_INDEX = Outcome(302, To.ADMIN)

# --- app screens (M2.6) -------------------------------------------------------------------

PROJECT_VIEWERS = (
    "group_admin",
    "mm_mad",
    "mm_sev",
    "wo_mad",
    "tech_mad",
    "site_manager",
    "partner",
    "mixed",
)
PROJECT_MANAGERS = ("group_admin", "mm_mad", "mm_sev", "mixed")
# Who sees each object position of a project URL (S1, S3, S4; technicians and partners only
# ACTIVE projects).
HOSPITAL_VIEWERS = (
    "group_admin",
    "mm_mad",
    "wo_mad",
    "tech_mad",
    "site_manager",
    "partner",
    "mixed",
)
SEV_VIEWERS = ("group_admin", "mm_sev", "mixed")
ON_HOLD_VIEWERS = ("group_admin", "mm_mad", "wo_mad")
HOSPITAL_MANAGERS = ("group_admin", "mm_mad")


def _each(outcome: Outcome, names: tuple[str, ...]) -> dict[str, Outcome]:
    return dict.fromkeys(names, outcome)


def app_page(**outcomes: Outcome) -> dict[str, Expect]:
    """A GET screen: login, session MFA for privileged users, then the view (403 default)."""
    row = expect(DENIED, signed_out=LOGIN, no_mfa=STEP_UP, **outcomes)
    return {"GET": row, "HEAD": row, "POST": expect(NOT_ALLOWED, no_mfa=DENIED)}


def app_action(**outcomes: Outcome) -> dict[str, Expect]:
    """A POST-only view that calls a service (F66): 405 to GET, login, then the view."""
    get = expect(NOT_ALLOWED, no_mfa=STEP_UP)
    return {"GET": get, "HEAD": get, "POST": expect(DENIED, signed_out=LOGIN, **outcomes)}


def project_page(
    capable: tuple[str, ...], visible: tuple[str, ...], allowed: tuple[str, ...]
) -> dict[str, Expect]:
    """``capable`` hold the capability somewhere (others 403), ``visible`` see the object
    (others of ``capable`` 404), ``allowed`` may act on it (others of ``visible`` 403)."""
    outcomes = _each(NOT_FOUND, capable) | _each(DENIED, visible) | _each(OK, allowed)
    return app_page(**{name: outcomes[name] for name in capable})


def project_action(
    capable: tuple[str, ...], visible: tuple[str, ...], allowed: tuple[str, ...], ok: Outcome
) -> dict[str, Expect]:
    outcomes = _each(NOT_FOUND, capable) | _each(DENIED, visible) | _each(ok, allowed)
    return app_action(**{name: outcomes[name] for name in capable})


def org_page() -> dict[str, Expect]:
    return app_page(group_admin=OK)


def org_action(ok: Outcome = REDIRECT) -> dict[str, Expect]:
    return app_action(group_admin=ok)


def token_link(to: To) -> dict[str, Expect]:
    """A token link: everybody is redirected to its token-less page (exempt from MFA)."""
    row = expect(Outcome(302, to))
    return {"GET": row, "HEAD": row, "POST": expect(NOT_ALLOWED)}


def token_page() -> dict[str, Expect]:
    """A token-less page without a token in the session: the generic error page."""
    row = expect(OK, no_mfa=STEP_UP)
    return {"GET": row, "HEAD": row, "POST": expect(NOT_ALLOWED, no_mfa=DENIED)}


def token_action() -> dict[str, Expect]:
    get = expect(NOT_ALLOWED, no_mfa=STEP_UP)
    return {"GET": get, "HEAD": get, "POST": expect(OK, no_mfa=DENIED)}


def _project_url(name: str, project: str) -> Callable[[World], str]:
    return lambda world: reverse(name, args=[getattr(world.org, project).pk])


def _object_url(name: str, key: str) -> Callable[[World], str]:
    return lambda world: reverse(name, args=[world.objects[key].pk])


def _person_url(name: str) -> Callable[[World], str]:
    return lambda world: reverse(name, args=[world.people.tech_mad.pk])


UNKNOWN_ID = "00000000-0000-7000-8000-000000000000"
# Every account with a valid membership reads the catalog; managers change it (M3.1).
CATALOG_VIEWERS = (*PROJECT_VIEWERS, "revoked_assignment")
CATALOG_MANAGERS = PROJECT_MANAGERS


# Suppliers are read by who holds supplier.view and changed by who holds supplier.manage; the
# mixed user manages through A-SEV (S6). Not site managers, technicians or partners.
SUPPLIER_VIEWERS = ("group_admin", "mm_mad", "mm_sev", "wo_mad", "mixed")
SUPPLIER_MANAGERS = PROJECT_MANAGERS


def _supplier_url(name: str) -> Callable[[World], str]:
    return lambda world: reverse(name, args=[world.org.supplier.pk])


def _unknown_url(name: str) -> Callable[[World], str]:
    return lambda world: reverse(name, args=[UNKNOWN_ID])


# Requests (M4.3): request.view reaches the lines of the procurement branch (S5) or of the
# assigned projects; request.create and request.withdraw only the user's own requests.
REQUEST_VIEWERS = PROJECT_VIEWERS
REQUEST_CREATORS = tuple(name for name in PROJECT_VIEWERS if name != "wo_mad")
# Who sees the technician's request (A-MAD buys for the hospital), and the partner's.
TECH_REQUEST_VIEWERS = ("group_admin", "mm_mad", "wo_mad", "site_manager", "tech_mad")
PARTNER_REQUEST_VIEWERS = ("group_admin", "mm_mad", "wo_mad", "site_manager", "partner")


def _line_url(name: str) -> Callable[[World], str]:
    return lambda world: reverse(name, args=[world.objects["materialrequestline"].pk])


def request_read(visible: tuple[str, ...]) -> dict[str, Expect]:
    """A request page: ``visible`` read it, other request viewers get 404."""
    return project_page(REQUEST_VIEWERS, visible, visible)


def request_own_page(visible: tuple[str, ...], owner: str) -> dict[str, Expect]:
    """A requester's page: 404 outside the scope, 403 for who sees but is not the owner."""
    return project_page(REQUEST_CREATORS, visible, (owner,))


def request_own_action(visible: tuple[str, ...], owner: str, ok: Outcome) -> dict[str, Expect]:
    return project_action(REQUEST_CREATORS, visible, (owner,), ok)


def removed_by_the_technician(row: dict[str, Expect]) -> dict[str, Expect]:
    """The actors after the technician (``ACTORS`` order) find the removed line gone."""
    return {**row, "POST": {**row["POST"], "site_manager": NOT_FOUND}}


def request_url(name: str, key: str) -> Callable[[World], str]:
    return _object_url(name, key)


PROJECT_POSITIONS = (
    ("own scope", "hospital", HOSPITAL_VIEWERS),
    ("foreign scope", "sev_works", SEV_VIEWERS),
    ("on hold", "mad_hold", ON_HOLD_VIEWERS),
)
FAKE_TOKEN = "A" * 43

APP_ROUTES: dict[str, tuple[Route, ...]] = {
    "projects:list": (Route(app_page(**_each(OK, PROJECT_VIEWERS))),),
    "projects:detail": tuple(
        Route(
            project_page(PROJECT_VIEWERS, visible, visible),
            position=position,
            path=_project_url("projects:detail", project),
        )
        for position, project, visible in PROJECT_POSITIONS
    ),
    "projects:new": (Route(app_page(**_each(OK, PROJECT_MANAGERS))),),
    "projects:create": (
        Route(app_action(**_each(OK, PROJECT_MANAGERS)), note="an empty form: 200 with errors"),
    ),
    "projects:edit": (
        Route(
            project_page(PROJECT_MANAGERS, HOSPITAL_VIEWERS, HOSPITAL_MANAGERS),
            position="own scope",
            path=_project_url("projects:edit", "hospital"),
        ),
        Route(
            project_page(PROJECT_MANAGERS, SEV_VIEWERS, SEV_VIEWERS),
            position="foreign scope",
            path=_project_url("projects:edit", "sev_works"),
        ),
    ),
    "projects:update": (
        Route(
            project_action(PROJECT_MANAGERS, HOSPITAL_VIEWERS, HOSPITAL_MANAGERS, OK),
            path=_project_url("projects:update", "hospital"),
            note="an empty form: 200 with errors",
        ),
    ),
    "projects:assign": (
        Route(
            project_action(PROJECT_MANAGERS, HOSPITAL_VIEWERS, HOSPITAL_MANAGERS, OK),
            path=_project_url("projects:assign", "hospital"),
            note="an empty form: the detail page with errors",
        ),
    ),
    "projects:revoke_assignment": (
        Route(
            project_action(PROJECT_MANAGERS, HOSPITAL_VIEWERS, HOSPITAL_MANAGERS, REDIRECT),
            path=_object_url("projects:revoke_assignment", "projectassignment"),
            note="the hospital's site manager; revoked once, then reported",
        ),
    ),
    "projects:assignment_validity": (
        Route(
            project_action(PROJECT_MANAGERS, HOSPITAL_VIEWERS, HOSPITAL_MANAGERS, REDIRECT),
            path=_object_url("projects:assignment_validity", "projectassignment"),
            note="an empty form: reported, then back to the project",
        ),
    ),
    # Catalog (M3.1): installation-wide master data. Every valid membership views it (also a
    # site manager whose assignments are all revoked); managers change it.
    "catalog:list": (Route(app_page(**_each(OK, CATALOG_VIEWERS))),),
    "catalog:search": (Route(app_page(**_each(OK, CATALOG_VIEWERS))),),
    "catalog:detail": (
        Route(
            app_page(**_each(OK, CATALOG_VIEWERS)),
            position="a material",
            path=_object_url("catalog:detail", "material"),
        ),
        Route(
            app_page(**_each(NOT_FOUND, CATALOG_VIEWERS)),
            position="unknown",
            path=_unknown_url("catalog:detail"),
        ),
    ),
    "catalog:new": (Route(app_page(**_each(OK, CATALOG_MANAGERS))),),
    "catalog:create": (
        Route(app_action(**_each(OK, CATALOG_MANAGERS)), note="an empty form: 200 with errors"),
    ),
    "catalog:edit": (
        Route(
            app_page(**_each(OK, CATALOG_MANAGERS)),
            position="a material",
            path=_object_url("catalog:edit", "material"),
        ),
        Route(
            app_page(**_each(NOT_FOUND, CATALOG_MANAGERS)),
            position="unknown",
            path=_unknown_url("catalog:edit"),
        ),
    ),
    "catalog:update": (
        Route(
            app_action(**_each(OK, CATALOG_MANAGERS)),
            path=_object_url("catalog:update", "material"),
            note="an empty form: 200 with errors",
        ),
        Route(
            app_action(**_each(NOT_FOUND, CATALOG_MANAGERS)),
            position="unknown",
            path=_unknown_url("catalog:update"),
        ),
    ),
    "catalog:deactivate": (
        Route(
            app_action(**_each(REDIRECT, CATALOG_MANAGERS)),
            path=_object_url("catalog:deactivate", "material"),
        ),
    ),
    "catalog:reactivate": (
        Route(
            app_action(**_each(REDIRECT, CATALOG_MANAGERS)),
            path=_object_url("catalog:reactivate", "material"),
        ),
    ),
    "catalog:categories": (Route(app_page(**_each(OK, CATALOG_VIEWERS))),),
    "catalog:category_new": (Route(app_page(**_each(OK, CATALOG_MANAGERS))),),
    "catalog:category_create": (
        Route(app_action(**_each(OK, CATALOG_MANAGERS)), note="an empty form: 200 with errors"),
    ),
    "catalog:category_edit": (
        Route(
            app_page(**_each(OK, CATALOG_MANAGERS)),
            path=_object_url("catalog:category_edit", "materialcategory"),
        ),
    ),
    "catalog:category_update": (
        Route(
            app_action(**_each(OK, CATALOG_MANAGERS)),
            path=_object_url("catalog:category_update", "materialcategory"),
            note="an empty form: 200 with errors",
        ),
    ),
    # Suppliers (M3.2): supplier.view (group administrators, material managers, warehouse
    # operators) reads every supplier; supplier.manage (not warehouse operators) changes them.
    # Site managers, technicians and partners get nothing: no supplier name, email or phone.
    # Requests (M4.3): the requester's screens. Out of scope 404, in scope but not the
    # requester's 403; a request is never shown to a role that does not reach it.
    "requests:list": (Route(app_page(**_each(OK, REQUEST_VIEWERS))),),
    "requests:new": (Route(app_page(**_each(OK, REQUEST_CREATORS))),),
    "requests:start": (
        Route(
            app_action(**_each(REDIRECT, REQUEST_CREATORS)),
            note="an empty form: back to the project list",
        ),
    ),
    "requests:detail": (
        Route(
            request_read(TECH_REQUEST_VIEWERS),
            position="the technician's draft",
            path=request_url("requests:detail", "materialrequest"),
        ),
        Route(
            request_read(PARTNER_REQUEST_VIEWERS),
            position="the partner's draft",
            path=request_url("requests:detail", "partner_request"),
        ),
        Route(
            request_read(()),
            position="unknown",
            path=_unknown_url("requests:detail"),
        ),
    ),
    "requests:materials": (
        Route(
            request_own_page(TECH_REQUEST_VIEWERS, "tech_mad"),
            position="own draft",
            path=request_url("requests:materials", "materialrequest"),
        ),
        Route(
            request_own_page(PARTNER_REQUEST_VIEWERS, "partner"),
            position="the partner's draft",
            path=request_url("requests:materials", "partner_request"),
        ),
    ),
    "requests:review": (
        Route(
            request_own_page(TECH_REQUEST_VIEWERS, "tech_mad"),
            position="own draft",
            path=request_url("requests:review", "materialrequest"),
        ),
        Route(
            request_own_page(PARTNER_REQUEST_VIEWERS, "partner"),
            position="the partner's draft",
            path=request_url("requests:review", "partner_request"),
        ),
    ),
    "requests:add_line": (
        Route(
            request_own_action(TECH_REQUEST_VIEWERS, "tech_mad", REDIRECT),
            position="own draft",
            path=request_url("requests:add_line", "materialrequest"),
            note="an empty form: back to the request",
        ),
    ),
    "requests:add_free_line": (
        Route(
            request_own_action(TECH_REQUEST_VIEWERS, "tech_mad", REDIRECT),
            position="own draft",
            path=request_url("requests:add_free_line", "materialrequest"),
            note="an empty form: back to the request",
        ),
    ),
    "requests:submit": (
        Route(
            request_own_action(TECH_REQUEST_VIEWERS, "tech_mad", OK),
            position="own draft",
            path=request_url("requests:submit", "materialrequest"),
            note="an empty form: 200 with errors",
        ),
    ),
    "requests:cancel": (
        Route(
            request_own_action(TECH_REQUEST_VIEWERS, "tech_mad", REDIRECT),
            position="own draft",
            path=request_url("requests:cancel", "materialrequest"),
        ),
        Route(
            request_own_action(PARTNER_REQUEST_VIEWERS, "partner", REDIRECT),
            position="the partner's draft",
            path=request_url("requests:cancel", "partner_request"),
        ),
    ),
    "requests:withdraw": (
        Route(
            request_own_action(TECH_REQUEST_VIEWERS, "tech_mad", REDIRECT),
            position="own submitted request",
            path=request_url("requests:withdraw", "submitted_request"),
        ),
    ),
    "requests:update_line": (
        Route(
            request_own_action(TECH_REQUEST_VIEWERS, "tech_mad", REDIRECT),
            position="a line of the own draft",
            path=_line_url("requests:update_line"),
            note="an empty form: back to the request",
        ),
    ),
    "requests:remove_line": (
        Route(
            removed_by_the_technician(
                request_own_action(TECH_REQUEST_VIEWERS, "tech_mad", REDIRECT)
            ),
            position="a line of the own draft",
            path=_line_url("requests:remove_line"),
            note="the site manager comes after the technician, who removes the line",
        ),
    ),
    "suppliers:list": (Route(app_page(**_each(OK, SUPPLIER_VIEWERS)), allowed=SUPPLIER_SENTINELS),),
    "suppliers:detail": (
        Route(
            app_page(**_each(OK, SUPPLIER_VIEWERS)),
            position="a supplier",
            path=_supplier_url("suppliers:detail"),
            allowed=SUPPLIER_SENTINELS,
        ),
        Route(
            app_page(**_each(NOT_FOUND, SUPPLIER_VIEWERS)),
            position="unknown",
            path=_unknown_url("suppliers:detail"),
        ),
    ),
    "suppliers:new": (Route(app_page(**_each(OK, SUPPLIER_MANAGERS))),),
    "suppliers:create": (
        Route(app_action(**_each(OK, SUPPLIER_MANAGERS)), note="an empty form: 200 with errors"),
    ),
    "suppliers:edit": (
        Route(
            app_page(**_each(OK, SUPPLIER_MANAGERS)),
            position="a supplier",
            path=_supplier_url("suppliers:edit"),
            allowed=SUPPLIER_SENTINELS,
        ),
        Route(
            app_page(**_each(NOT_FOUND, SUPPLIER_MANAGERS)),
            position="unknown",
            path=_unknown_url("suppliers:edit"),
        ),
    ),
    "suppliers:update": (
        Route(
            app_action(**_each(OK, SUPPLIER_MANAGERS)),
            path=_supplier_url("suppliers:update"),
            allowed=SUPPLIER_SENTINELS,
            note="an empty form: 200 with errors",
        ),
        Route(
            app_action(**_each(NOT_FOUND, SUPPLIER_MANAGERS)),
            position="unknown",
            path=_unknown_url("suppliers:update"),
        ),
    ),
    "suppliers:deactivate": (
        Route(
            app_action(**_each(REDIRECT, SUPPLIER_MANAGERS)),
            path=_supplier_url("suppliers:deactivate"),
        ),
    ),
    "suppliers:reactivate": (
        Route(
            app_action(**_each(REDIRECT, SUPPLIER_MANAGERS)),
            path=_supplier_url("suppliers:reactivate"),
        ),
    ),
    # Group administrators only (org.manage); the target is the A-MAD technician.
    "org:people": (Route(org_page()),),
    "org:person": (Route(org_page(), path=_person_url("org:person")),),
    "org:grant_membership": (Route(org_action(), path=_person_url("org:grant_membership")),),
    "org:set_employer": (Route(org_action(), path=_person_url("org:set_employer")),),
    "org:offboard": (
        Route(org_action(), path=_person_url("org:offboard"), note="unconfirmed: nothing happens"),
    ),
    "org:request_email_change": (
        Route(org_action(), path=_person_url("org:request_email_change")),
    ),
    "org:revoke_membership": (
        Route(org_action(), path=_object_url("org:revoke_membership", "membership")),
    ),
    "org:membership_validity": (
        Route(org_action(), path=_object_url("org:membership_validity", "membership")),
    ),
    "org:revoke_email_change": (
        Route(org_action(), path=_object_url("org:revoke_email_change", "emailchangerequest")),
    ),
    "org:invitations": (Route(org_page()),),
    "org:new_invitation": (Route(org_page()),),
    "org:create_invitation": (Route(org_action(OK), note="an empty form: 200 with errors"),),
    "org:resend_invitation": (
        Route(org_action(), path=_object_url("org:resend_invitation", "invitation")),
    ),
    "org:revoke_invitation": (
        Route(org_action(), path=_object_url("org:revoke_invitation", "invitation")),
    ),
    # Token pages: anonymous by design; no token in the session shows the generic page.
    "invitations:link": (
        Route(
            token_link(To.INVITATION),
            path=lambda world: reverse("invitations:link", args=[FAKE_TOKEN]),
        ),
    ),
    "invitations:accept": (Route(token_page()),),
    "invitations:submit": (Route(token_action()),),
    "email_change:link": (
        Route(
            token_link(To.EMAIL_CHANGE),
            path=lambda world: reverse("email_change:link", args=[FAKE_TOKEN]),
        ),
    ),
    "email_change:confirm": (Route(token_page()),),
    "email_change:confirm_submit": (Route(token_action()),),
    "email_change:cancel": (Route(token_page()),),
    "email_change:cancel_submit": (Route(token_action()),),
}

# --- the table ---------------------------------------------------------------------------

ROUTES: dict[str, tuple[Route, ...]] = {
    key: (route,)
    for key, route in {
        # Health checks (Caddy, compose): everybody, GET/HEAD only.
        "core:healthz": Route(public()),
        "core:readyz": Route(public()),
        # The landing page: login, then session MFA for privileged users.
        "core:home": Route(
            same(
                expect(OK, signed_out=LOGIN, no_mfa=STEP_UP),
                expect(NOT_ALLOWED, no_mfa=DENIED),
            )
        ),
        "set_language": Route(public(HOME, HOME)),
        # allauth: login, logout and password reset are reachable signed out.
        "account_login": Route(same(expect(HOME, signed_out=OK))),
        "account_logout": Route(same(expect(OK, signed_out=HOME), expect(HOME))),
        "account_inactive": Route(public()),
        "account_reauthenticate": Route(signed_in(OK)),
        "account_change_password": Route(signed_in(OK)),
        "account_reset_password": Route(public(OK, OK)),
        "account_reset_password_done": Route(public()),
        "account_reset_password_from_key": Route(
            public(OK, OK),
            path=lambda world: reverse(
                "account_reset_password_from_key", kwargs={"uidb36": "zz", "key": "set-password"}
            ),
            note="an unknown key shows the invalid-link page",
        ),
        "account_reset_password_from_key_done": Route(public()),
        # allauth MFA: signed in; factor changes ask for reauthentication first.
        "mfa_index": Route(signed_in(OK, NOT_ALLOWED)),
        "mfa_authenticate": Route(signed_in(HOME), note="no login in progress"),
        "mfa_reauthenticate": Route(signed_in(OK)),
        "mfa_activate_totp": Route(signed_in(REAUTHENTICATE)),
        "mfa_deactivate_totp": Route(signed_in(REAUTHENTICATE)),
        "mfa_view_recovery_codes": Route(signed_in(NOT_FOUND, NOT_ALLOWED), note="none yet"),
        "mfa_generate_recovery_codes": Route(signed_in(REAUTHENTICATE)),
        "mfa_download_recovery_codes": Route(signed_in(NOT_FOUND), note="none yet"),
        "mfa_list_webauthn": Route(signed_in(OK, NOT_ALLOWED)),
        "mfa_add_webauthn": Route(signed_in(REAUTHENTICATE)),
        "mfa_reauthenticate_webauthn": Route(signed_in(REAUTHENTICATE)),
        "mfa_remove_webauthn": Route(
            signed_in(REAUTHENTICATE),
            path=lambda world: reverse("mfa_remove_webauthn", kwargs={"pk": 999_999}),
        ),
        "mfa_edit_webauthn": Route(
            signed_in(REAUTHENTICATE),
            path=lambda world: reverse("mfa_edit_webauthn", kwargs={"pk": 999_999}),
        ),
        # The admin site.
        "admin:index": Route(admin_page(OK, OK)),
        "admin:app_list": Route(
            admin_page(OK, OK),
            path=lambda world: reverse("admin:app_list", kwargs={"app_label": "organizations"}),
        ),
        "admin:login": Route(
            same(
                expect(
                    DENIED,
                    signed_out=LOGIN,
                    admitted=ADMIN_INDEX,
                    group_admin_no_mfa=ADMIN_INDEX,
                )
            ),
            note="no Django login form: allauth's login, then 403 unless admitted",
        ),
        "admin:logout": Route(
            same(
                expect(ADMIN_INDEX, admitted=Outcome(302, To.LOGOUT)),
                expect(ADMIN_INDEX, admitted=Outcome(307, To.LOGOUT)),
            ),
            note="hands over to allauth's logout",
        ),
        "admin:password_change": Route(
            admin_page(Outcome(302, To.CHANGE_PASSWORD), Outcome(302, To.CHANGE_PASSWORD)),
            note="hands over to allauth's password change",
        ),
        "admin:password_change_done": Route(
            admin_page(Outcome(302, To.CHANGE_PASSWORD), Outcome(302, To.CHANGE_PASSWORD))
        ),
        "admin:autocomplete": Route(admin_page(DENIED, NOT_ALLOWED), note="no autocomplete field"),
        "admin:jsi18n": Route(admin_page(OK, NOT_ALLOWED)),
        "admin:view_on_site": Route(
            admin_page(NOT_FOUND, NOT_FOUND),
            path=lambda world: reverse(
                "admin:view_on_site",
                args=[ContentType.objects.get_for_model(world.org.a_mad).pk, world.org.a_mad.pk],
            ),
            note="no model has a public URL",
        ),
        "admin:(?P<url>.*)$": Route(
            admin_page(NOT_FOUND, NOT_FOUND),
            path=lambda world: f"/{settings.ADMIN_URL_PATH}/nothing/here/",
        ),
        **_admin_model_routes(),
    }.items()
} | APP_ROUTES


# --- the matrix --------------------------------------------------------------------------


def test_every_url_has_a_row_and_every_row_a_url() -> None:
    missing, stale = missing_rows(url_patterns(), set(ROUTES))
    assert missing == [], f"URL patterns without a row in ROUTES: {missing}"
    assert stale == [], f"Rows without a URL pattern: {stale}"


@pytest.mark.parametrize("key", sorted(ROUTES))
def test_every_row_covers_every_method_and_actor(key: str) -> None:
    for route in ROUTES[key]:
        assert set(route.methods) == set(TABLE_METHODS), key
        for method, outcomes in route.methods.items():
            assert set(outcomes) == set(ACTOR_NAMES), f"{method} {key}"


@pytest.mark.parametrize("key", sorted(ROUTES))
def test_route(world: World, key: str) -> None:
    problems = [problem for route in ROUTES[key] for problem in check_route(key, route, world)]
    assert problems == [], "\n".join(problems)


@pytest.mark.parametrize("key", sorted(ROUTES))
def test_other_methods_are_refused_everywhere(world: World, key: str) -> None:
    """S7: anything but GET, HEAD and POST is a 405, signed in or not."""
    url = ROUTES[key][0].url(key, world)
    for actor in ("anonymous", "group_admin", "tech_mad"):
        for method in OTHER_METHODS:
            response = send(world.client(actor), method, url)
            assert response.status_code == 405, f"{method} {key} as {actor}"
            assert response["Allow"] == "GET, HEAD, POST"


# --- admin (plan §8.2) -------------------------------------------------------------------


class TestAdmin:
    def test_every_admin_is_service_backed(self) -> None:
        for model, model_admin in admin.site._registry.items():
            assert isinstance(model_admin, ServiceBackedModelAdmin), model

    def test_access_and_document_models_are_read_only_and_the_rest_master_data(self) -> None:
        registered = {
            (model._meta.app_label, model._meta.model_name): model_admin
            for model, model_admin in admin.site._registry.items()
        }
        read_only = {(app, m) for app, names in READ_ONLY_MODELS.items() for m in names}
        master = {(app, m) for app, names in MASTER_DATA_MODELS.items() for m in names}
        assert set(registered) == read_only | master
        for key in read_only:
            assert isinstance(registered[key], ReadOnlyModelAdmin), key
        for key in master:
            assert isinstance(registered[key], CapabilityModelAdmin), key

    def test_no_admin_offers_the_bulk_delete(self, world: World) -> None:
        client = world.client("group_admin")
        for model in admin.site._registry:
            url = reverse(f"admin:{model._meta.app_label}_{model._meta.model_name}_changelist")
            response = client.get(url)
            assert response.status_code == 200, url
            assert b'value="delete_selected"' not in response.content, url

    def test_a_master_data_save_goes_through_its_service(self, world: World) -> None:
        """Deactivating company C (``is_active`` left unticked): generation bumped, audited."""
        generation = AccessState.objects.get().org_generation
        response = world.client("group_admin").post(
            reverse("admin:organizations_company_change", args=[world.org.c.pk]),
            {"legal_name": "Company C", "kind": "STANDARD", "tax_id": ""},
        )
        assert response.status_code == 302
        assert Company.objects.get(pk=world.org.c.pk).is_active is False
        assert AccessState.objects.get().org_generation > generation
        assert AuditEvent.objects.filter(action="company.updated", object_id=world.org.c.pk)

    def test_a_group_admin_cannot_touch_a_staff_account(self, world: World) -> None:
        operator = world.people.operator
        client = world.client("group_admin")
        change = reverse("admin:accounts_user_change", args=[operator.pk])
        response = client.post(
            change, {"email": "hijack@example.org", "is_staff": "", "is_superuser": ""}
        )
        assert response.status_code == 403
        operator.refresh_from_db()
        assert (operator.email, operator.is_staff, operator.is_superuser) == (
            "operator@example.org",
            True,
            True,
        )


# --- revoked between page load and submit (plan §8.2) ------------------------------------


def _expire(world: World) -> None:
    Membership.objects.filter(user=world.people.group_admin).update(
        valid_until=timezone.now() - timedelta(seconds=1)
    )


def _revoke(world: World) -> None:
    Membership.objects.filter(user=world.people.group_admin).update(revoked_at=timezone.now())


def _password(world: World) -> None:
    User.objects.filter(pk=world.people.group_admin.pk).update(password=make_password("new-pw-1"))


def _email(world: World) -> None:
    User.objects.filter(pk=world.people.group_admin.pk).update(email="moved@example.org")


def _mfa_reset(world: World) -> None:
    Authenticator.objects.filter(user=world.people.group_admin).delete()
    bump_session_epoch(world.people.group_admin)


def _deactivate(world: World) -> None:
    User.objects.filter(pk=world.people.group_admin.pk).update(is_active=False)


def _elevate(world: World) -> None:
    User.objects.filter(pk=world.people.group_admin.pk).update(
        privilege_elevated_at=timezone.now() + timedelta(seconds=5)
    )


@pytest.mark.parametrize(
    ("change", "outcome"),
    [
        (_expire, ADMIN_LOGIN),
        (_revoke, ADMIN_LOGIN),
        (_password, ADMIN_LOGIN),
        (_email, ADMIN_LOGIN),
        (_mfa_reset, ADMIN_LOGIN),
        (_deactivate, ADMIN_LOGIN),
        (_elevate, DENIED),
    ],
    ids=[
        "membership-expired",
        "revoked",
        "password",
        "email",
        "mfa-reset",
        "deactivated",
        "elevated-without-step-up",
    ],
)
def test_a_change_between_page_load_and_submit_refuses_the_write(
    world: World, change: Callable[[World], None], outcome: Outcome
) -> None:
    client = world.client("group_admin")
    url = reverse("admin:organizations_company_change", args=[world.org.a.pk])
    assert client.get(url).status_code == 200
    change(world)
    events = AuditEvent.objects.count()
    response = client.post(url, {"legal_name": "Hijacked", "kind": "STANDARD", "is_active": "on"})
    assert outcome.matches(response), (response.status_code, response.get("Location"))
    assert Company.objects.get(pk=world.org.a.pk).legal_name == "Company A"
    assert AuditEvent.objects.count() == events


def test_the_stale_session_actor_really_is_stale(world: World) -> None:
    """The fixture's premise: the same user's current session is still signed in."""
    assert world.client("tech_mad").get(reverse("core:home")).status_code == 200
    assert LOGIN.matches(world.client("stale_session").get(reverse("core:home")))

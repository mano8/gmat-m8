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

The account pages (allauth), the admin (purchase orders included, M5.1; receipts and
handovers, M6.1), the health checks, the token pages (invitations, email changes), the
screens of M2.6 (projects, people, invitations), the master-data screens of M3 (catalog,
suppliers), the request screens of M4 and the purchasing screens of M5.2 and M5.3 have
rows; later business screens add theirs with the steps that build them. The price is
allowed only to its ``price.view`` holders (``Route.allowed_for``), so a warehouse
operator reading an order is checked priceless.
"""

import io
import re
from collections.abc import Callable
from dataclasses import replace
from datetime import timedelta
from typing import Any

import pytest
from allauth.mfa.models import Authenticator
from django.conf import settings
from django.contrib import admin
from django.contrib.auth.hashers import make_password
from django.contrib.contenttypes.models import ContentType
from django.core.files.uploadedfile import SimpleUploadedFile
from django.urls import reverse
from django.utils import timezone
from PIL import Image

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
    BAD_REQUEST,
    DENIED,
    GROUPS,
    HOME,
    LOGIN,
    NO_CONTENT,
    NOT_ALLOWED,
    NOT_FOUND,
    OK,
    OTHER_METHODS,
    REAUTHENTICATE,
    REDIRECT,
    SEE_OTHER,
    SEE_OTHER_HOME,
    STEP_UP,
    TABLE_METHODS,
    UNPROCESSABLE,
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

# The header htmx sends: a dialog's fragment or form (M8b.3) is asked for with it.
HTMX = {"HX-Request": "true"}


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
    "purchasing": ("purchaseorder", "purchaseorderline", "purchaseallocation"),
    "receiving": ("goodsreceipt", "goodsreceiptline", "receiptattribution"),
    "handovers": ("handover", "handoverline"),
    "attachments": ("attachment", "attachmentreservation", "storagequota", "mediadeletion"),
}
MASTER_DATA_MODELS = {
    "organizations": ("company", "jointventuremember", "branch"),
    "locations": ("location",),
    "catalog": ("materialcategory", "material"),
    "suppliers": ("supplier",),
}
# The admin pages of a model that shows its own sentinels: the supplier's contact details
# (also in an order's supplier snapshot) and the allocations' quantities. Prices never
# reach the admin (``apps.purchasing.admin``).
ADMIN_SENTINELS = {
    "supplier": SUPPLIER_SENTINELS,
    "purchaseorder": SUPPLIER_SENTINELS,
    "purchaseallocation": frozenset({Sentinel.OTHER_PROJECT_QUANTITY}),
}


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
    "engineer",
    "wo_mad",
    "tech_mad",
    "site_manager",
    "partner",
    "mixed",
)
PROJECT_MANAGERS = ("group_admin", "mm_mad", "engineer", "mm_sev", "mixed")
# Who sees each object position of a project URL (S1, S3, S4; technicians and partners only
# ACTIVE projects).
HOSPITAL_VIEWERS = (
    "group_admin",
    "mm_mad",
    "engineer",
    "wo_mad",
    "tech_mad",
    "site_manager",
    "partner",
    "mixed",
)
SEV_VIEWERS = ("group_admin", "mm_sev", "mixed")
ON_HOLD_VIEWERS = ("group_admin", "mm_mad", "engineer", "wo_mad")
HOSPITAL_MANAGERS = ("group_admin", "mm_mad", "engineer")


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


def own_account_action() -> dict[str, Expect]:
    """A POST-only action on the signed-in account itself: no capability involved."""
    get = expect(NOT_ALLOWED, no_mfa=STEP_UP)
    return {"GET": get, "HEAD": get, "POST": expect(REDIRECT, signed_out=LOGIN, no_mfa=DENIED)}


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
SUPPLIER_VIEWERS = ("group_admin", "mm_mad", "engineer", "mm_sev", "wo_mad", "mixed")
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
TECH_REQUEST_VIEWERS = (
    "group_admin",
    "mm_mad",
    "engineer",
    "wo_mad",
    "site_manager",
    "tech_mad",
)
PARTNER_REQUEST_VIEWERS = (
    "group_admin",
    "mm_mad",
    "engineer",
    "wo_mad",
    "site_manager",
    "partner",
)


def _line_url(name: str) -> Callable[[World], str]:
    return lambda world: reverse(name, args=[world.objects["materialrequestline"].pk])


# Who sees the hospital project through request.view: A-MAD's buyers and the assigned users.
HOSPITAL_REQUEST_VIEWERS = (
    "group_admin",
    "mm_mad",
    "engineer",
    "mixed",
    "wo_mad",
    "site_manager",
    "tech_mad",
    "partner",
)


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


# The reviewer's screens (M4.4). request.review is held somewhere by group administrators,
# material managers, engineers (M7b.1: everything a material manager holds; the fixture's is
# A-MAD's) and site managers; request.source (and cancel_any) not by site managers.
# On the technician's request at the hospital (bought for by A-MAD) the readers are those
# whose scope or assignment reaches it; the material manager of A-SEV holds the capability
# elsewhere and gets 404, a warehouse operator or the technician none (403).
REVIEWERS = ("group_admin", "mm_mad", "mm_sev", "engineer", "site_manager", "mixed")
SOURCERS = ("group_admin", "mm_mad", "engineer", "mm_sev", "mixed")
REVIEW_READERS = ("group_admin", "mm_mad", "engineer", "site_manager")
SOURCE_READERS = ("group_admin", "mm_mad", "engineer")


def review_page() -> dict[str, Expect]:
    return project_page(REVIEWERS, REVIEW_READERS, REVIEW_READERS)


def review_action(ok: Outcome = REDIRECT) -> dict[str, Expect]:
    return project_action(REVIEWERS, REVIEW_READERS, REVIEW_READERS, ok)


def source_page() -> dict[str, Expect]:
    return project_page(SOURCERS, SOURCE_READERS, SOURCE_READERS)


def source_action(ok: Outcome = REDIRECT) -> dict[str, Expect]:
    return project_action(SOURCERS, SOURCE_READERS, SOURCE_READERS, ok)


def line_of(name: str, key: str) -> Callable[[World], str]:
    return lambda world: reverse(name, args=[world.objects[key].pk])


# Purchasing (M5.2): po.view (group administrators, material managers, warehouse operators)
# reads the orders of its branches; po.manage (not warehouse operators) builds and changes
# them. The orders of the fixture are A-MAD's: the material manager of A-SEV and the mixed
# user (a manager at A-SEV only) hold the capability elsewhere and get 404.
PO_VIEWERS = ("group_admin", "mm_mad", "engineer", "mm_sev", "wo_mad", "mixed")
PO_MANAGERS = ("group_admin", "mm_mad", "engineer", "mm_sev", "mixed")
PO_READERS = ("group_admin", "mm_mad", "engineer", "wo_mad")
PO_ACTORS = ("group_admin", "mm_mad", "engineer")
# Price (and the quantities of other projects) a purchase screen may show: the manager's.
PRICE_VIEWERS = {name: frozenset({Sentinel.PRICE}) for name in PO_ACTORS}
PO_SENTINELS = SUPPLIER_SENTINELS | {Sentinel.OTHER_PROJECT_QUANTITY}


# Receiving and handovers (M6.3). receipt.view / receipt.post / handover.view: group
# administrators, material managers, warehouse operators and site managers (the mixed user is a
# manager at A-SEV). The hospital is A-MAD's to buy for and B-MAD's to run, so its receipts and
# handovers are read by the A-MAD staff and its site manager; receipt.void and handover.void are
# the managers' and the engineer's (the first three of ``WHOLE_ORDER``); handover.post excludes
# site managers.
RECEIPT_HOLDERS = ("group_admin", "mm_mad", "engineer", "mm_sev", "wo_mad", "site_manager", "mixed")
RECEIPT_VOIDERS = ("group_admin", "mm_mad", "engineer", "mm_sev", "mixed")
HANDOVER_POSTERS = ("group_admin", "mm_mad", "engineer", "mm_sev", "wo_mad", "mixed")
WHOLE_ORDER = ("group_admin", "mm_mad", "engineer", "wo_mad")
HOSPITAL_RECEIVERS = ("group_admin", "mm_mad", "engineer", "wo_mad", "site_manager")
SUPPLIER_NAME = frozenset({Sentinel.SUPPLIER_NAME})
OTHER_QUANTITY = frozenset({Sentinel.OTHER_PROJECT_QUANTITY})


# Dashboard panels (M7.2): a fragment per panel, for whoever holds its capability somewhere;
# the rows inside come from scoped selectors (the unit tests check them per scope).
PANEL_HOLDERS: dict[str, tuple[str, ...]] = {
    "my-requests": REQUEST_VIEWERS,
    "inbox": REVIEWERS,
    "assigned": SOURCERS,
    "sourcing": SOURCERS,
    "to-purchase": PO_MANAGERS,
    "stale-drafts": PO_MANAGERS,
    "unallocated": PO_MANAGERS,
    "awaiting-receipt": PO_VIEWERS,
    "overdue": PO_VIEWERS,
    "to-hand-over": HANDOVER_POSTERS,
    "site-surplus": RECEIPT_HOLDERS,
    "closed-attention": SOURCERS,
    "dispatches": PO_MANAGERS,
    "overview": ("group_admin",),
}
# An unknown panel is a 404 for every signed-in session that passed the MFA guard.
SESSION_ACTORS = tuple(
    name for name in ACTOR_NAMES if name not in (*GROUPS["signed_out"], *GROUPS["no_mfa"])
)


def _panel_url(key: str) -> Callable[[World], str]:
    return lambda world: reverse("dashboard:panel", kwargs={"key": key})


def dashboard_routes() -> tuple[Route, ...]:
    panels = tuple(
        Route(
            app_page(**_each(OK, holders)),
            position=key,
            path=_panel_url(key),
            allowed=SUPPLIER_NAME | OTHER_QUANTITY,
        )
        for key, holders in PANEL_HOLDERS.items()
    )
    unknown = Route(
        app_page(**_each(NOT_FOUND, SESSION_ACTORS)),
        position="unknown panel",
        path=_panel_url("no-such-panel"),
    )
    return (*panels, unknown)


def ui_preference_routes() -> tuple[Route, ...]:
    """``/ui/preferences/`` (M8b.2, Q69): open to everyone, including a privileged session without
    MFA (it writes only the display cookie): GET and HEAD are refused, a POST answers 303 back (or
    204 to htmx), a value outside the allowlist 400, a foreign ``next`` falls back to the home page.
    No actor sees a sentinel: it renders nothing of the application's data."""

    def post(outcome: Outcome, **data: str) -> Route:
        return Route(
            {
                "GET": expect(NOT_ALLOWED),
                "HEAD": expect(NOT_ALLOWED),
                "POST": expect(outcome),
            },
            position=" ".join(f"{key}={value}" for key, value in data.items()) or "empty",
            data=lambda world: dict(data),
        )

    htmx = replace(
        post(NO_CONTENT, theme="dark", next="/requests/"),
        position="htmx",
        headers={"HX-Request": "true"},
    )
    return (
        post(SEE_OTHER),
        post(SEE_OTHER, theme="dark", next="/requests/"),
        post(SEE_OTHER, theme="system"),
        post(SEE_OTHER, menu="hidden"),
        post(SEE_OTHER, fold="insights", state="closed"),
        htmx,
        post(SEE_OTHER_HOME, theme="light", next="https://evil.example/"),
        post(BAD_REQUEST, theme="purple"),
        post(BAD_REQUEST, fold="nowhere", state="open"),
    )


# Audit (M7.3): audit.view is the group administrators' and the material managers'; an event
# belongs to its branch, so a manager gets 404 for another branch's object history. Engineers
# hold both, like material managers (ADR-0014).
AUDIT_HOLDERS = ("group_admin", "mm_mad", "mm_sev", "engineer", "mixed")
AUDIT_MAD_READERS = ("group_admin", "mm_mad", "engineer")
AUDIT_SEV_READERS = ("group_admin", "mm_sev", "mixed")
EXPORT_KEYS = ("requests", "orders", "receipts")


def _audit_url(key: str) -> Callable[[World], str]:
    return lambda world: reverse(
        "audit:object",
        args=[world.objects[key].object_type, world.objects[key].object_id],
    )


def _export_url(key: str) -> Callable[[World], str]:
    return lambda world: reverse("exports:download", kwargs={"key": key})


def audit_routes() -> tuple[Route, ...]:
    def history(readers: tuple[str, ...]) -> dict[str, Expect]:
        return order_page(AUDIT_HOLDERS, readers)

    return (
        Route(history(AUDIT_MAD_READERS), position="own branch", path=_audit_url("audit_mad")),
        Route(history(AUDIT_SEV_READERS), position="other branch", path=_audit_url("audit_sev")),
        Route(
            history(()),
            position="unknown object",
            path=lambda world: reverse("audit:object", args=["fixture.nothing", UNKNOWN_ID]),
        ),
    )


def export_routes() -> tuple[Route, ...]:
    downloads = [
        Route(
            app_page(**_each(OK, AUDIT_HOLDERS)),
            position=key,
            path=_export_url(key),
            allowed=SUPPLIER_NAME,
            allowed_for=PRICE_VIEWERS,
        )
        for key in EXPORT_KEYS
    ]
    return (
        *downloads,
        Route(
            app_page(**_each(NOT_FOUND, AUDIT_HOLDERS)),
            position="unknown export",
            path=lambda world: "/exports/nothing.csv",
        ),
    )


def order_page(
    capable: tuple[str, ...], readers: tuple[str, ...], ok: Outcome = OK
) -> dict[str, Expect]:
    """A page of one order: 404 outside its scope, ``ok`` inside; others of ``capable`` only."""
    outcomes = _each(NOT_FOUND, capable) | _each(ok, readers)
    return app_page(**{name: outcomes[name] for name in capable})


def order_read(ok: Outcome = OK) -> dict[str, Expect]:
    return order_page(PO_VIEWERS, PO_READERS, ok)


def order_manage(ok: Outcome = OK) -> dict[str, Expect]:
    return order_page(PO_MANAGERS, PO_ACTORS, ok)


def order_action(ok: Outcome = REDIRECT) -> dict[str, Expect]:
    return project_action(PO_MANAGERS, PO_ACTORS, PO_ACTORS, ok)


def purchase_url(name: str, key: str) -> Callable[[World], str]:
    return _object_url(name, key)


# Attachments (M6.4): every operation is checked against the parent (plan §5). View with the
# parent's view capability; add with request.create (the requester, on a draft),
# receipt.post or handover.post; remove by the uploader on a draft, else with request.source,
# receipt.void or handover.void (the managers). Removal rows post no reason: a manager is sent
# back to the page with the error and nothing changes, so every actor meets the same file;
# the technician removes their own draft's file, which needs none.
UPLOAD_ORIGIN = {"Origin": "http://testserver"}
FILE_MANAGERS = ("group_admin", "mm_mad", "engineer")


def _upload_body(world: World) -> dict[str, Any]:
    buffer = io.BytesIO()
    Image.new("RGB", (8, 8), "red").save(buffer, "PNG")
    return {"file": SimpleUploadedFile("photo.png", buffer.getvalue())}


def _download_unknown() -> dict[str, Expect]:
    """An unknown attachment is not found for anyone signed in (its parent decides 403)."""
    row = expect(NOT_FOUND, signed_out=LOGIN, no_mfa=STEP_UP)
    return {"GET": row, "HEAD": row, "POST": expect(NOT_ALLOWED, no_mfa=DENIED)}


def _upload(name: str, key: str) -> Callable[[World], str]:
    return _object_url(name, key)


def _attachment_url(name: str, key: str) -> Callable[[World], str]:
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
    # M7.1: every member (catalog.view) sets their own business-email choice.
    "notifications:settings": (Route(app_page(**_each(OK, CATALOG_VIEWERS))),),
    "notifications:save": (Route(app_action(**_each(REDIRECT, CATALOG_VIEWERS))),),
    # M8.3 (X5): every signed-in account, whatever its memberships; session MFA as everywhere.
    # Only one's own rows are reached: someone else's id answers as an ended session does.
    "sessions:list": (
        Route(
            same(expect(OK, signed_out=LOGIN, no_mfa=STEP_UP), expect(NOT_ALLOWED, no_mfa=DENIED))
        ),
    ),
    "sessions:end_others": (Route(own_account_action()),),
    "sessions:end": (
        Route(
            own_account_action(),
            path=lambda world: reverse("sessions:end", kwargs={"session_id": 999_999}),
            note="no such session: flashed, then back to the list",
        ),
    ),
    "dashboard:panel": dashboard_routes(),
    "core:ui_preferences": ui_preference_routes(),
    "audit:list": (Route(app_page(**_each(OK, AUDIT_HOLDERS))),),
    "audit:object": audit_routes(),
    "exports:index": (Route(app_page(**_each(OK, AUDIT_HOLDERS))),),
    "exports:download": export_routes(),
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
    "requests:ledger": (
        Route(
            request_read(TECH_REQUEST_VIEWERS),
            position="the technician's draft",
            path=request_url("requests:ledger", "materialrequest"),
        ),
        Route(
            request_read(PARTNER_REQUEST_VIEWERS),
            position="the partner's draft",
            path=request_url("requests:ledger", "partner_request"),
        ),
        Route(
            request_read(()),
            position="unknown",
            path=_unknown_url("requests:ledger"),
        ),
    ),
    "requests:project_materials": (
        Route(
            project_page(REQUEST_VIEWERS, HOSPITAL_REQUEST_VIEWERS, HOSPITAL_REQUEST_VIEWERS),
            position="own scope",
            path=_project_url("requests:project_materials", "hospital"),
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
    # The reviewer (M4.4): inbox and review page, then one POST per action. An empty form is
    # invalid and changes nothing (back to the page), except approve-all and cancel, which
    # take no input and have a request of their own.
    "requests:inbox": (Route(app_page(**_each(OK, REVIEWERS))),),
    "requests:examine": (
        Route(
            review_page(),
            position="the technician's submitted request",
            path=request_url("requests:examine", "review_request"),
        ),
        Route(
            review_page(),
            position="an approved request",
            path=request_url("requests:examine", "approved_request"),
        ),
        Route(
            project_page(REVIEWERS, (), ()),
            position="unknown",
            path=_unknown_url("requests:examine"),
        ),
    ),
    "requests:approve_all": (
        Route(
            review_action(),
            position="a submitted request",
            path=request_url("requests:approve_all", "approve_request"),
            note="no page version: back to the page, nothing approved",
        ),
    ),
    "requests:return_to_draft": (
        Route(
            review_action(),
            position="a submitted request",
            path=request_url("requests:return_to_draft", "review_request"),
            note="an empty note: back to the page",
        ),
        Route(
            review_action(UNPROCESSABLE),
            position="a submitted request, htmx",
            path=request_url("requests:return_to_draft", "review_request"),
            headers=HTMX,
            note="an empty note from the dialog: the form again with its errors",
        ),
    ),
    "requests:decide_line": (
        Route(
            review_action(),
            position="a line of a submitted request",
            path=line_of("requests:decide_line", "review_request_line"),
            note="an empty form: back to the page",
        ),
    ),
    "requests:reduce_line": (
        Route(
            review_action(),
            position="a line of an approved request",
            path=line_of("requests:reduce_line", "approved_request_line"),
            note="an empty form: back to the page",
        ),
        Route(
            review_action(UNPROCESSABLE),
            position="a line of an approved request, htmx",
            path=line_of("requests:reduce_line", "approved_request_line"),
            headers=HTMX,
            note="an empty form from the dialog: the form again with its errors",
        ),
    ),
    "requests:set_sourcing": (
        Route(
            source_action(),
            position="a line of an approved request",
            path=line_of("requests:set_sourcing", "approved_request_line"),
            note="an empty form: back to the page",
        ),
    ),
    "requests:link": (
        Route(
            source_page(),
            position="a non-catalog line",
            path=line_of("requests:link", "free_line"),
        ),
    ),
    "requests:resolve_line": (
        Route(
            source_action(),
            position="a non-catalog line",
            path=line_of("requests:resolve_line", "free_line"),
            note="an empty form: back to the dialog",
        ),
    ),
    "requests:close_line": (
        Route(
            source_action(),
            position="a line of an approved request",
            path=line_of("requests:close_line", "approved_request_line"),
            note="no reason: back to the page",
        ),
        Route(
            source_action(UNPROCESSABLE),
            position="a line of an approved request, htmx",
            path=line_of("requests:close_line", "approved_request_line"),
            headers=HTMX,
            note="no reason from the dialog: the form again with its errors",
        ),
    ),
    "requests:reopen_line": (
        Route(
            source_action(),
            position="a line of an approved request",
            path=line_of("requests:reopen_line", "approved_request_line"),
            note="no reason: back to the page",
        ),
        Route(
            source_action(UNPROCESSABLE),
            position="a line of an approved request, htmx",
            path=line_of("requests:reopen_line", "approved_request_line"),
            headers=HTMX,
            note="no reason from the dialog: the form again with its errors",
        ),
    ),
    "requests:schedule": (
        Route(
            source_action(),
            position="an approved request",
            path=request_url("requests:schedule", "approved_request"),
            note="an empty form: back to the page",
        ),
    ),
    "requests:close": (
        Route(
            source_action(),
            position="an approved request",
            path=request_url("requests:close", "approved_request"),
            note="no reason: back to the page",
        ),
        Route(
            source_action(UNPROCESSABLE),
            position="an approved request, htmx",
            path=request_url("requests:close", "approved_request"),
            headers=HTMX,
            note="no reason from the dialog: the form again with its errors",
        ),
    ),
    "requests:reopen": (
        Route(
            source_action(),
            position="an approved request",
            path=request_url("requests:reopen", "approved_request"),
            note="no reason: back to the page",
        ),
        Route(
            source_action(UNPROCESSABLE),
            position="an approved request, htmx",
            path=request_url("requests:reopen", "approved_request"),
            headers=HTMX,
            note="no reason from the dialog: the form again with its errors",
        ),
    ),
    "requests:cancel_any": (
        Route(
            source_action(),
            position="a submitted request",
            path=request_url("requests:cancel_any", "cancel_request"),
            note="the first reviewer cancels it, the others find it cancelled",
        ),
    ),
    # Reviewer assignment and edits (M7b.4): assigning takes request.review (site managers
    # included, the choices come from the selector); editing takes request.edit, which group
    # administrators, material managers and engineers hold: the same readers as sourcing.
    "requests:assign_reviewer": (
        Route(
            review_action(),
            position="a submitted request",
            path=request_url("requests:assign_reviewer", "review_request"),
            note="an empty form: back to the page",
        ),
        Route(
            review_action(UNPROCESSABLE),
            position="a submitted request, htmx",
            path=request_url("requests:assign_reviewer", "review_request"),
            headers=HTMX,
            note="an empty form from the dialog: the form again with its errors",
        ),
    ),
    "requests:edit_line": (
        Route(
            source_page(),
            position="a live line of an approved request",
            path=line_of("requests:edit_line", "approved_request_line"),
        ),
        # M8b.3's lazy dialog pilot: the review page's dialog asks for the same URL with htmx
        # and gets its fragment; the outcomes and sentinels are the plain page's.
        Route(
            source_page(),
            position="a live line of an approved request, htmx",
            path=line_of("requests:edit_line", "approved_request_line"),
            headers=HTMX,
        ),
    ),
    "requests:apply_edit": (
        Route(
            source_action(),
            position="a live line of an approved request",
            path=line_of("requests:apply_edit", "approved_request_line"),
            note="an empty form: back to the dialog",
        ),
        Route(
            source_action(UNPROCESSABLE),
            position="a live line of an approved request, htmx",
            path=line_of("requests:apply_edit", "approved_request_line"),
            headers=HTMX,
            note="an empty form from the dialog: the form again with its errors",
        ),
    ),
    "requests:new_line": (
        Route(
            source_page(),
            position="an approved request",
            path=request_url("requests:new_line", "approved_request"),
        ),
    ),
    "requests:apply_add": (
        Route(
            source_action(),
            position="an approved request",
            path=request_url("requests:apply_add", "approved_request"),
            note="an empty form: back to the dialog",
        ),
    ),
    "requests:remove_reviewed_line": (
        Route(
            source_action(),
            position="a live line of an approved request",
            path=line_of("requests:remove_reviewed_line", "approved_request_line"),
            note="no reason: back to the page",
        ),
        Route(
            source_action(UNPROCESSABLE),
            position="a live line of an approved request, htmx",
            path=line_of("requests:remove_reviewed_line", "approved_request_line"),
            headers=HTMX,
            note="no reason from the dialog: the form again with its errors",
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
    # Purchasing (M5.2): the issued order is the sentinel one (priced, other project's
    # quantity); the draft is a plain one. An empty POST changes nothing (invalid or refused).
    "purchasing:worklist": (
        Route(
            app_page(**_each(OK, PO_MANAGERS)),
            allowed=PO_SENTINELS,
            note="the supplier filter lists suppliers; the SEV-2 line is bought by A-MAD",
        ),
    ),
    "purchasing:orders": (Route(app_page(**_each(OK, PO_VIEWERS)), allowed=SUPPLIER_SENTINELS),),
    "purchasing:new": (Route(app_page(**_each(OK, PO_MANAGERS)), allowed=SUPPLIER_SENTINELS),),
    "purchasing:create": (
        Route(
            app_action(**_each(OK, PO_MANAGERS)),
            allowed=SUPPLIER_SENTINELS,
            note="an empty form: 200 with errors",
        ),
    ),
    "purchasing:detail": (
        Route(
            order_read(),
            position="issued",
            path=purchase_url("purchasing:detail", "purchaseorder"),
            allowed=PO_SENTINELS,
            allowed_for=PRICE_VIEWERS,
        ),
        Route(
            order_read(),
            position="draft",
            path=purchase_url("purchasing:detail", "draftorder"),
            allowed=SUPPLIER_SENTINELS,
        ),
        Route(
            app_page(**_each(NOT_FOUND, PO_VIEWERS)),
            position="unknown",
            path=_unknown_url("purchasing:detail"),
        ),
    ),
    "purchasing:print": (
        Route(
            order_read(),
            position="issued",
            path=purchase_url("purchasing:print", "purchaseorder"),
            allowed=PO_SENTINELS,
            allowed_for=PRICE_VIEWERS,
        ),
        Route(
            order_read(REDIRECT),
            position="draft",
            path=purchase_url("purchasing:print", "draftorder"),
            note="a draft is not printed",
        ),
    ),
    "purchasing:amendment": (
        Route(
            order_manage(),
            position="issued",
            path=purchase_url("purchasing:amendment", "purchaseorder"),
            allowed=PO_SENTINELS,
            allowed_for=PRICE_VIEWERS,
        ),
        Route(
            order_manage(REDIRECT),
            position="draft",
            path=purchase_url("purchasing:amendment", "draftorder"),
            note="only an issued order is amended",
        ),
    ),
    "purchasing:update": (
        Route(order_action(), path=purchase_url("purchasing:update", "draftorder")),
        Route(
            order_action(UNPROCESSABLE),
            position="htmx",
            path=purchase_url("purchasing:update", "draftorder"),
            headers=HTMX,
            allowed=SUPPLIER_SENTINELS,
            note="an empty form from the dialog: the form again with its errors",
        ),
    ),
    "purchasing:add_lines": (
        Route(order_action(), path=purchase_url("purchasing:add_lines", "draftorder")),
        Route(
            order_action(UNPROCESSABLE),
            position="htmx",
            path=purchase_url("purchasing:add_lines", "draftorder"),
            headers=HTMX,
            note="nothing ticked from the dialog: the form again with its errors",
        ),
    ),
    "purchasing:issue": (
        Route(
            order_action(),
            path=purchase_url("purchasing:issue", "draftorder"),
            note="unconfirmed: nothing happens",
        ),
    ),
    "purchasing:cancel": (
        Route(
            order_action(),
            path=purchase_url("purchasing:cancel", "draftorder"),
            note="the first allowed actor discards the draft; the others are refused",
        ),
    ),
    "purchasing:amend": (
        Route(
            order_action(OK),
            path=purchase_url("purchasing:amend", "purchaseorder"),
            allowed=PO_SENTINELS,
            allowed_for=PRICE_VIEWERS,
            note="an empty form: 200 with errors",
        ),
    ),
    "purchasing:update_line": (
        Route(order_action(), path=purchase_url("purchasing:update_line", "draftline")),
        Route(
            order_action(UNPROCESSABLE),
            position="htmx",
            path=purchase_url("purchasing:update_line", "draftline"),
            headers=HTMX,
            note="an empty form from the dialog: the form again with its errors",
        ),
    ),
    "purchasing:remove_line": (
        Route(
            order_action(),
            path=purchase_url("purchasing:remove_line", "purchaseorderline"),
            note="an issued line: refused, nothing is removed",
        ),
        Route(
            order_action(UNPROCESSABLE),
            position="htmx",
            path=purchase_url("purchasing:remove_line", "purchaseorderline"),
            headers=HTMX,
            note="an issued line: refused on the dialog's form, nothing is removed",
        ),
    ),
    "purchasing:close_line": (
        Route(order_action(), path=purchase_url("purchasing:close_line", "purchaseorderline")),
        Route(
            order_action(UNPROCESSABLE),
            position="htmx",
            path=purchase_url("purchasing:close_line", "purchaseorderline"),
            headers=HTMX,
            note="no reason from the dialog: the form again with its errors",
        ),
    ),
    "purchasing:increase_allocation": (
        Route(
            order_action(),
            path=purchase_url("purchasing:increase_allocation", "draftallocation"),
        ),
        Route(
            order_action(UNPROCESSABLE),
            position="htmx",
            path=purchase_url("purchasing:increase_allocation", "draftallocation"),
            headers=HTMX,
            note="an empty form from the dialog: the form again with its errors",
        ),
    ),
    "purchasing:unallocate": (
        Route(
            order_action(),
            path=purchase_url("purchasing:unallocate", "draftallocation"),
        ),
        Route(
            order_action(UNPROCESSABLE),
            position="htmx",
            path=purchase_url("purchasing:unallocate", "draftallocation"),
            headers=HTMX,
            note="an empty form from the dialog: the form again with its errors",
        ),
    ),
    # M5.3: the issued order's dispatch, left AMBIGUOUS by the worker.
    "purchasing:confirm_dispatch": (
        Route(
            order_action(),
            path=purchase_url("purchasing:confirm_dispatch", "dispatch"),
            note="the first allowed actor confirms it; the others are refused",
        ),
        Route(
            app_action(**_each(NOT_FOUND, PO_MANAGERS)),
            position="unknown",
            path=_unknown_url("purchasing:confirm_dispatch"),
        ),
    ),
    "purchasing:resend_dispatch": (
        Route(
            order_action(),
            path=purchase_url("purchasing:resend_dispatch", "dispatch"),
            note="queued again, or refused once confirmed",
        ),
    ),
    # Receiving (M6.3): receipt.view reads receipts at a warehouse (branch scope) or at a
    # worksite (its project: the site manager too); receipt.post receives; receipt.void is the
    # manager's. The site manager's pages hold the supplier's name and nothing else of the order.
    "receiving:list": (Route(app_page(**_each(OK, RECEIPT_HOLDERS))),),
    "receiving:orders": (Route(app_page(**_each(OK, RECEIPT_HOLDERS)), allowed=SUPPLIER_NAME),),
    "receiving:projects": (Route(app_page(**_each(OK, RECEIPT_HOLDERS))),),
    "receiving:expected": (
        Route(
            project_page(RECEIPT_HOLDERS, HOSPITAL_RECEIVERS, HOSPITAL_RECEIVERS),
            position="own scope",
            path=_project_url("receiving:expected", "hospital"),
            allowed=SUPPLIER_NAME,
            note="the sentinel order: the supplier's name and the hospital's 10, never the "
            "price, the contacts or the other project's quantity",
        ),
        Route(
            project_page(RECEIPT_HOLDERS, SEV_VIEWERS, SEV_VIEWERS),
            position="foreign scope",
            path=_project_url("receiving:expected", "sev_works"),
        ),
    ),
    "receiving:receive": (
        Route(
            project_page(RECEIPT_HOLDERS, HOSPITAL_RECEIVERS, HOSPITAL_RECEIVERS),
            position="issued",
            path=purchase_url("receiving:receive", "purchaseorder"),
            allowed=SUPPLIER_NAME,
            allowed_for=dict.fromkeys(WHOLE_ORDER, OTHER_QUANTITY),
            note="the site manager gets their project's share only: not the order's outstanding "
            "quantity nor the other project's allocation",
        ),
        Route(
            project_page(RECEIPT_HOLDERS, WHOLE_ORDER, WHOLE_ORDER),
            position="draft",
            path=purchase_url("receiving:receive", "draftorder"),
            note="a draft is nobody's to receive: the page offers nothing, and a site "
            "manager does not know it exists",
        ),
        Route(
            app_page(**_each(NOT_FOUND, RECEIPT_HOLDERS)),
            position="unknown",
            path=_unknown_url("receiving:receive"),
        ),
    ),
    "receiving:post": (
        Route(
            project_action(RECEIPT_HOLDERS, HOSPITAL_RECEIVERS, HOSPITAL_RECEIVERS, REDIRECT),
            path=purchase_url("receiving:post", "purchaseorder"),
            note="no place chosen: back to the form",
        ),
        Route(
            app_action(**_each(NOT_FOUND, RECEIPT_HOLDERS)),
            position="unknown",
            path=_unknown_url("receiving:post"),
        ),
    ),
    "receiving:detail": (
        Route(
            project_page(RECEIPT_HOLDERS, HOSPITAL_RECEIVERS, HOSPITAL_RECEIVERS),
            position="worksite",
            path=_object_url("receiving:detail", "goodsreceipt"),
            allowed=SUPPLIER_NAME,
            note="the site manager reads the receipt of their worksite",
        ),
        Route(
            project_page(RECEIPT_HOLDERS, WHOLE_ORDER, WHOLE_ORDER),
            position="warehouse",
            path=_object_url("receiving:detail", "warehousereceipt"),
            allowed=SUPPLIER_NAME,
            note="a site manager has no warehouse receipt",
        ),
        Route(
            app_page(**_each(NOT_FOUND, RECEIPT_HOLDERS)),
            position="unknown",
            path=_unknown_url("receiving:detail"),
        ),
    ),
    "receiving:void": (
        Route(
            project_action(
                RECEIPT_VOIDERS,
                ("group_admin", "mm_mad", "engineer"),
                ("group_admin", "mm_mad", "engineer"),
                REDIRECT,
            ),
            position="worksite",
            path=_object_url("receiving:void", "goodsreceipt"),
            note="no reason: back to the receipt",
        ),
        Route(
            project_action(
                RECEIPT_VOIDERS,
                ("group_admin", "mm_mad", "engineer"),
                ("group_admin", "mm_mad", "engineer"),
                REDIRECT,
            ),
            position="warehouse",
            path=_object_url("receiving:void", "warehousereceipt"),
        ),
        # M8b.3's inline dialog pilot: the void form posted with htmx.
        Route(
            project_action(
                RECEIPT_VOIDERS,
                ("group_admin", "mm_mad", "engineer"),
                ("group_admin", "mm_mad", "engineer"),
                UNPROCESSABLE,
            ),
            position="worksite, htmx",
            path=_object_url("receiving:void", "goodsreceipt"),
            headers=HTMX,
            note="no reason from the dialog: the form again with its errors",
        ),
        Route(
            project_action(
                RECEIPT_VOIDERS,
                ("group_admin", "mm_mad", "engineer"),
                ("group_admin", "mm_mad", "engineer"),
                UNPROCESSABLE,
            ),
            position="warehouse, htmx",
            path=_object_url("receiving:void", "warehousereceipt"),
            headers=HTMX,
        ),
    ),
    # Handovers (M6.3): handover.view also reaches an assigned project (the site manager reads,
    # never posts); handover.post and handover.void are the branch's.
    "handovers:list": (Route(app_page(**_each(OK, RECEIPT_HOLDERS))),),
    "handovers:new": (
        Route(app_page(**_each(OK, HANDOVER_POSTERS)), position="no project"),
        Route(
            project_page(HANDOVER_POSTERS, WHOLE_ORDER, WHOLE_ORDER),
            position="a project",
            path=lambda world: f"{reverse('handovers:new')}?project={world.org.hospital.pk}",
        ),
        Route(
            project_page(HANDOVER_POSTERS, WHOLE_ORDER, WHOLE_ORDER),
            position="a request",
            path=lambda world: (
                f"{reverse('handovers:new')}?request={world.objects['approved_request'].pk}"
            ),
        ),
    ),
    "handovers:create": (
        Route(
            app_action(**_each(REDIRECT, HANDOVER_POSTERS)),
            position="no project",
            note="back to the choice of a project",
        ),
        Route(
            project_action(HANDOVER_POSTERS, WHOLE_ORDER, WHOLE_ORDER, OK),
            position="a project",
            data=lambda world: {"project": str(world.org.hospital.pk)},
            note="an empty form: 200 with errors",
        ),
    ),
    "handovers:detail": (
        Route(
            project_page(RECEIPT_HOLDERS, HOSPITAL_RECEIVERS, HOSPITAL_RECEIVERS),
            position="from a warehouse",
            path=_object_url("handovers:detail", "warehousehandover"),
        ),
        Route(
            project_page(RECEIPT_HOLDERS, HOSPITAL_RECEIVERS, HOSPITAL_RECEIVERS),
            position="received on site",
            path=_object_url("handovers:detail", "handover"),
        ),
        Route(
            app_page(**_each(NOT_FOUND, RECEIPT_HOLDERS)),
            position="unknown",
            path=_unknown_url("handovers:detail"),
        ),
    ),
    "handovers:void": (
        Route(
            project_action(
                RECEIPT_VOIDERS,
                ("group_admin", "mm_mad", "engineer"),
                ("group_admin", "mm_mad", "engineer"),
                REDIRECT,
            ),
            position="from a warehouse",
            path=_object_url("handovers:void", "warehousehandover"),
            note="no reason: back to the handover",
        ),
        Route(
            project_action(
                RECEIPT_VOIDERS,
                ("group_admin", "mm_mad", "engineer"),
                ("group_admin", "mm_mad", "engineer"),
                REDIRECT,
            ),
            position="received on site",
            path=_object_url("handovers:void", "handover"),
            note="no reason: back to the handover (it goes with its receipt)",
        ),
    ),
    "attachments:upload_request": (
        Route(
            request_own_action(TECH_REQUEST_VIEWERS, "tech_mad", REDIRECT),
            position="the technician's draft",
            path=_upload("attachments:upload_request", "materialrequest"),
            data=_upload_body,
            headers=UPLOAD_ORIGIN,
            note="only the requester adds to their draft; back to the request",
        ),
    ),
    "attachments:upload_receipt": (
        Route(
            project_action(RECEIPT_HOLDERS, HOSPITAL_RECEIVERS, HOSPITAL_RECEIVERS, REDIRECT),
            position="worksite",
            path=_upload("attachments:upload_receipt", "goodsreceipt"),
            data=_upload_body,
            headers=UPLOAD_ORIGIN,
            note="the site manager adds to the receipt of their worksite",
        ),
        Route(
            project_action(RECEIPT_HOLDERS, WHOLE_ORDER, WHOLE_ORDER, REDIRECT),
            position="warehouse",
            path=_upload("attachments:upload_receipt", "warehousereceipt"),
            data=_upload_body,
            headers=UPLOAD_ORIGIN,
        ),
    ),
    "attachments:upload_handover": (
        Route(
            project_action(HANDOVER_POSTERS, WHOLE_ORDER, WHOLE_ORDER, REDIRECT),
            position="from a warehouse",
            path=_upload("attachments:upload_handover", "warehousehandover"),
            data=_upload_body,
            headers=UPLOAD_ORIGIN,
            note="the site manager reads the handover but never posts one",
        ),
    ),
    "attachments:download": (
        Route(
            request_read(TECH_REQUEST_VIEWERS),
            position="the technician's draft",
            path=_attachment_url("attachments:download", "requestattachment"),
        ),
        Route(
            project_page(RECEIPT_HOLDERS, HOSPITAL_RECEIVERS, HOSPITAL_RECEIVERS),
            position="worksite receipt",
            path=_attachment_url("attachments:download", "attachment"),
        ),
        Route(
            project_page(RECEIPT_HOLDERS, WHOLE_ORDER, WHOLE_ORDER),
            position="warehouse receipt",
            path=_attachment_url("attachments:download", "warehouseattachment"),
        ),
        Route(
            project_page(RECEIPT_HOLDERS, HOSPITAL_RECEIVERS, HOSPITAL_RECEIVERS),
            position="handover",
            path=_attachment_url("attachments:download", "handoverattachment"),
        ),
        Route(_download_unknown(), position="unknown", path=_unknown_url("attachments:download")),
    ),
    "attachments:remove": (
        Route(
            project_action(
                REQUEST_VIEWERS, TECH_REQUEST_VIEWERS, (*FILE_MANAGERS, "tech_mad"), REDIRECT
            ),
            position="the technician's draft",
            path=_attachment_url("attachments:remove", "requestattachment"),
            note="the uploader removes from the draft; the managers need a reason",
        ),
        Route(
            project_action(RECEIPT_HOLDERS, HOSPITAL_RECEIVERS, FILE_MANAGERS, REDIRECT),
            position="worksite receipt",
            path=_attachment_url("attachments:remove", "attachment"),
        ),
        Route(
            project_action(RECEIPT_HOLDERS, HOSPITAL_RECEIVERS, FILE_MANAGERS, REDIRECT),
            position="handover",
            path=_attachment_url("attachments:remove", "handoverattachment"),
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
    "org:invitation_delivery": (
        Route(org_page(), path=_object_url("org:invitation_delivery", "invitation")),
    ),
    "org:email_change_delivery": (
        Route(org_page(), path=_object_url("org:email_change_delivery", "emailchangerequest")),
    ),
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

# M7d.2: every paged list answers the same to every actor on a later page of the largest size.
PAGED_LISTS = (
    "catalog:list",
    "suppliers:list",
    "projects:list",
    "org:people",
    "org:invitations",
    "requests:list",
    "requests:new",
    "requests:inbox",
    "purchasing:orders",
    "receiving:list",
    "receiving:orders",
    "receiving:projects",
    "handovers:list",
    "audit:list",
)


def _later_page(key: str, route: Route) -> Route:
    return replace(
        route,
        position="page 2, 100 rows",
        path=lambda world: f"{reverse(key)}?per_page=100&page=2",
    )


for _key in PAGED_LISTS:
    ROUTES[_key] = (*ROUTES[_key], _later_page(_key, ROUTES[_key][0]))

# M7d.3: the screens that render a page of sections answer the same to every actor with the
# paging and filter parameters of each section, for every object position they have.
PAGED_SECTIONS = {
    "purchasing:worklist": (
        "per_page=100&after=5.abcdef12&back=0,&drafts_page=2&unallocated_after=3.abcdef12&priority=URGENT"
    ),
    "receiving:expected": "per_page=100&page=2&supplier=x&expected_by=2030-01-01",
    "requests:project_materials": "per_page=100&page=2&q=x&outstanding=on",
    "projects:detail": "assignments_per_page=100&assignments_page=2&assignments-state=revoked",
    "org:person": "memberships_page=2&projects_page=2&invitations_page=2&memberships-state=revoked",
    "audit:object": "per_page=100&page=2&q=x&since=2030-01-01",
}


def _with_query(key: str, route: Route, query: str) -> Route:
    def path(world: World) -> str:
        url = route.url(key, world)
        return f"{url}{'&' if '?' in url else '?'}{query}"

    return replace(
        route, position=f"{route.position} with {query.split('&')[0]}".strip(), path=path
    )


for _key, _query in PAGED_SECTIONS.items():
    ROUTES[_key] = (*ROUTES[_key], *(_with_query(_key, route, _query) for route in ROUTES[_key]))


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

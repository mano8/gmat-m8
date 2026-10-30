"""The machinery of the access acceptance matrix (plan §8.2).

- ``url_patterns()``: every URL pattern of the project, keyed by URL name (namespaced), or
  by its route inside the namespace when it has no name;
- ``World`` and ``ACTORS``: the shared fixture of plan §8.1 with one saved session per
  actor, built once per test module;
- ``Outcome`` / ``Expect``: what an actor must get, as a status plus where a redirect goes;
- ``Route``: one table row per URL, with one expectation per method and the sentinels its
  responses may contain. Every other sentinel is checked absent from every response.

The table itself is ``test_acceptance_matrix.py``.
"""

from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from django.conf import settings
from django.core.cache import cache
from django.test import Client
from django.urls import URLPattern, URLResolver, get_resolver, reverse

from apps.accounts.models import User
from tests.org import (
    Org,
    People,
    Sentinel,
    build_org,
    build_people,
    change_password_later,
    session_for,
)

# The methods a view can be asked for. ``AllowedMethodsMiddleware`` answers 405 to the
# others on every URL (checked by its own test), so a row lists these three.
TABLE_METHODS = ("GET", "HEAD", "POST")
OTHER_METHODS = ("PUT", "PATCH", "DELETE", "OPTIONS", "TRACE")


# --- URL inventory ---------------------------------------------------------------------


@dataclass(frozen=True)
class Pattern:
    key: str
    route: str


def _walk(
    resolver: URLResolver, namespace: str, prefix: str
) -> Iterator[tuple[str, str, URLPattern]]:
    for item in resolver.url_patterns:
        if isinstance(item, URLResolver):
            if item.namespace:
                inner = f"{namespace}:{item.namespace}" if namespace else item.namespace
                yield from _walk(item, inner, "")
            else:
                yield from _walk(item, namespace, prefix + str(item.pattern))
        else:
            yield namespace, prefix + str(item.pattern), item


def url_patterns() -> list[Pattern]:
    """Every URL pattern, keyed ``namespace:name`` (``namespace:route`` without a name)."""
    found: list[Pattern] = []
    for namespace, route, item in _walk(get_resolver(), "", ""):
        label = item.name or route
        found.append(Pattern(f"{namespace}:{label}" if namespace else label, route))
    return found


# --- world and actors ------------------------------------------------------------------


@dataclass(frozen=True)
class Actor:
    """Who sends the request: a fixture user and the state of their session.

    ``mfa``: the session passed MFA. ``stale``: the user's password changed after the
    session started (its epoch is behind). ``user=None``: anonymous.
    """

    name: str
    user: str | None
    mfa: bool = True
    stale: bool = False


ACTORS: tuple[Actor, ...] = (
    Actor("anonymous", None),
    Actor("group_admin", "group_admin"),
    Actor("group_admin_no_mfa", "group_admin", mfa=False),
    Actor("operator", "operator"),
    Actor("mm_mad", "mm_mad"),
    Actor("mm_mad_no_mfa", "mm_mad", mfa=False),
    Actor("mm_sev", "mm_sev"),
    Actor("wo_mad", "wo_mad"),
    Actor("tech_mad", "tech_mad", mfa=False),
    Actor("site_manager", "site_manager", mfa=False),
    Actor("partner", "partner", mfa=False),
    Actor("mixed", "mixed"),
    Actor("expired", "expired", mfa=False),
    Actor("revoked", "revoked", mfa=False),
    Actor("revoked_assignment", "revoked_assignment", mfa=False),
    Actor("inactive_partner", "inactive_partner", mfa=False),
    Actor("inactive_branch", "inactive_branch", mfa=False),
    Actor("deactivated", "deactivated", mfa=False),
    Actor("stale_session", "tech_mad", mfa=False, stale=True),
    Actor("nobody", "nobody", mfa=False),
)
ACTOR_NAMES = tuple(actor.name for actor in ACTORS)


@dataclass
class World:
    org: Org
    people: People
    sessions: dict[str, str] = field(default_factory=dict)  # actor -> session key
    objects: dict[str, Any] = field(default_factory=dict)  # named extra rows

    def user(self, name: str) -> User:
        user: User = getattr(self.people, name)
        return user

    def client(self, actor: str) -> Client:
        client = Client()
        key = self.sessions.get(actor)
        if key is not None:
            client.cookies[settings.SESSION_COOKIE_NAME] = key
        return client


def build_world(extra: Callable[[World], None] | None = None) -> World:
    """The organization, its people, and a session per actor.

    Stale sessions start first; their user's password then changes, and the other
    sessions of that user start after it.
    """
    org = build_org()
    world = World(org, build_people(org))
    signed_in = [actor for actor in ACTORS if actor.user is not None]
    for actor in sorted(signed_in, key=lambda actor: not actor.stale):
        user = world.user(str(actor.user))
        world.sessions[actor.name] = session_for(user, mfa=actor.mfa)
        if actor.stale:
            change_password_later(user)
    if extra is not None:
        extra(world)
    return world


# --- outcomes --------------------------------------------------------------------------


class To(Enum):
    """Where a redirect must go: the path of ``Location``, without its query."""

    LOGIN = "account_login"
    ADMIN_LOGIN = "admin:login"
    STEP_UP = "mfa_reauthenticate"
    REAUTHENTICATE = "account_reauthenticate"
    CHANGE_PASSWORD = "account_change_password"
    LOGOUT = "account_logout"
    ADMIN = "admin:index"
    HOME = "core:home"
    ANYWHERE = ""

    def matches(self, location: str) -> bool:
        if self is To.ANYWHERE:
            return bool(location)
        return location.split("?", 1)[0] == reverse(self.value)


@dataclass(frozen=True)
class Outcome:
    status: int
    to: To | None = None

    def __str__(self) -> str:
        return f"{self.status} → {self.to.name}" if self.to else str(self.status)

    def matches(self, response: Any) -> bool:
        if response.status_code != self.status:
            return False
        if self.to is None:
            return True
        return self.to.matches(response.get("Location", ""))


OK = Outcome(200)
LOGIN = Outcome(302, To.LOGIN)
ADMIN_LOGIN = Outcome(302, To.ADMIN_LOGIN)
STEP_UP = Outcome(302, To.STEP_UP)
REAUTHENTICATE = Outcome(302, To.REAUTHENTICATE)
HOME = Outcome(302, To.HOME)
REDIRECT = Outcome(302, To.ANYWHERE)
DENIED = Outcome(403)
NOT_FOUND = Outcome(404)
NOT_ALLOWED = Outcome(405)


type Expect = Mapping[str, Outcome]


def expect(default: Outcome, **overrides: Outcome) -> dict[str, Outcome]:
    """One outcome per actor: ``default``, except the named actors (or groups)."""
    outcomes = dict.fromkeys(ACTOR_NAMES, default)
    for name, outcome in overrides.items():
        names = GROUPS.get(name, (name,))
        for actor in names:
            if actor not in outcomes:
                raise KeyError(f"Unknown actor {actor!r}")
            outcomes[actor] = outcome
    return outcomes


# Groups for ``expect``.
GROUPS: dict[str, tuple[str, ...]] = {
    # Signed out in effect: anonymous, and the sessions the guard ends.
    "signed_out": ("anonymous", "deactivated", "stale_session"),
    # The admin site's users (an active superuser, a valid GROUP_ADMIN), with session MFA.
    "admitted": ("group_admin", "operator"),
    # Privileged users whose session has not passed MFA: step-up on every page.
    "no_mfa": ("group_admin_no_mfa", "mm_mad_no_mfa"),
    # Sessions that passed MFA but are not admitted to the admin.
    "mfa_not_admitted": ("mm_mad", "mm_sev", "wo_mad", "mixed"),
}


@dataclass(frozen=True)
class Route:
    """One URL: how to build it, what each method returns per actor, which sentinels.

    ``path(world)`` builds the URL (default: ``reverse(key)``). ``methods`` has a row for
    each of ``TABLE_METHODS``. ``data(world)`` is the POST body (default: empty). A URL
    with an object lists one ``Route`` per object position (own scope, managed branch,
    foreign scope, own or someone else's document), named by ``position``.
    """

    methods: Mapping[str, Expect]
    position: str = ""
    path: Callable[[World], str] | None = None
    data: Callable[[World], dict[str, Any]] | None = None
    allowed: frozenset[Sentinel] = frozenset()
    note: str = ""

    def url(self, key: str, world: World) -> str:
        return self.path(world) if self.path is not None else reverse(key)


def send(client: Client, method: str, url: str, data: dict[str, Any] | None = None) -> Any:
    """One request, independent of the ones before: rate-limit counters start empty."""
    cache.clear()
    if method == "POST":
        return client.post(url, data or {})
    return client.generic(method, url)


def leaked(response: Any, allowed: frozenset[Sentinel]) -> list[Sentinel]:
    """The forbidden sentinels in a response (HTML, partial, CSV, JSON alike)."""
    if getattr(response, "streaming", False):
        body = b"".join(response.streaming_content).decode("utf-8", "replace")
    else:
        body = response.content.decode("utf-8", "replace")
    return [
        sentinel for sentinel in Sentinel if sentinel not in allowed and sentinel.found_in(body)
    ]


def check_route(
    key: str, route: Route, world: World, actors: tuple[str, ...] = ACTOR_NAMES
) -> list[str]:
    """Send every method as every actor; return one line per mismatch or leak."""
    problems: list[str] = []
    url = route.url(key, world)
    label = f"{key} ({route.position})" if route.position else key
    for method in TABLE_METHODS:
        expected = route.methods[method]
        for actor in actors:
            data = route.data(world) if route.data is not None and method == "POST" else None
            response = send(world.client(actor), method, url, data)
            outcome = expected[actor]
            if not outcome.matches(response):
                got = f"{response.status_code} {response.get('Location', '')}".strip()
                problems.append(f"{method} {label} as {actor}: expected {outcome}, got {got}")
            for sentinel in leaked(response, route.allowed):
                problems.append(f"{method} {label} as {actor}: leaks {sentinel.name}")
    return problems


def missing_rows(patterns: list[Pattern], keys: set[str]) -> tuple[list[str], list[str]]:
    """URL patterns without a row, and rows without a URL pattern."""
    known = {pattern.key for pattern in patterns}
    return sorted(known - keys), sorted(keys - known)

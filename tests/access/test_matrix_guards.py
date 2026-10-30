"""The matrix fails on each deliberate violation of M2.4's "done when" (plan §11).

``tests.access.urls_violations`` adds views that commit them to the project's URLs. The
static half (``scripts/check_access_rules.py``) has its own cases in
``tests/unit/config/test_check_access_rules.py::TestDoneWhenViolations``.
"""

from collections.abc import Callable
from typing import Any

import pytest
from django.contrib import admin
from django.contrib.admin import ModelAdmin
from django.db.models import Model
from django.http import StreamingHttpResponse
from django.urls import reverse

from apps.access import principals
from apps.access.exceptions import PrincipalNotAllowed
from apps.core import checks
from apps.locations.models import Location
from apps.projects.models import Project
from tests.access.matrix import (
    DENIED,
    NOT_ALLOWED,
    NOT_FOUND,
    OK,
    Route,
    World,
    check_route,
    expect,
    leaked,
    missing_rows,
    url_patterns,
)
from tests.access.test_acceptance_matrix import ROUTES, same
from tests.org import Sentinel

pytestmark = [pytest.mark.django_db, pytest.mark.urls("tests.access.urls_violations")]

# Material managers of A-MAD (A-MAD + B-MAD) and A-SEV, and a technician of A-MAD.
TRIO = ("mm_mad", "mm_sev", "tech_mad")


def project_rows(name: str) -> tuple[Route, ...]:
    """A project page per object position, as a ``project.view`` detail must answer."""

    def at(project: str) -> Callable[[World], str]:
        return lambda world: reverse(name, args=[getattr(world.org, project).pk])

    return (
        # A-MAD's own project, on hold: technicians see active projects only.
        Route(same(expect(NOT_FOUND, mm_mad=OK)), path=at("mad_hold"), position="own"),
        Route(
            same(expect(NOT_FOUND, mm_mad=OK, tech_mad=OK)),
            path=at("hospital"),
            position="managed branch",
        ),
        Route(same(expect(NOT_FOUND, mm_sev=OK)), path=at("sev_works"), position="foreign"),
    )


def problems(key: str, routes: tuple[Route, ...], world: World) -> list[str]:
    return [p for route in routes for p in check_route(key, route, world, TRIO)]


class TestUnscopedView:
    def test_a_scoped_view_passes(self, world: World) -> None:
        assert problems("demo_project", project_rows("demo_project"), world) == []

    def test_an_unscoped_view_fails(self, world: World) -> None:
        found = problems("demo_project_unscoped", project_rows("demo_project_unscoped"), world)
        assert "GET demo_project_unscoped (foreign) as mm_mad: expected 404, got 200" in found


def test_an_unregistered_url_fails() -> None:
    missing, stale = missing_rows(url_patterns(), set(ROUTES))
    assert set(missing) == {
        "demo_project",
        "demo_project_unscoped",
        "demo_price",
        "demo_hold",
        "demo_hold_on_get",
        "demo_worker",
    }
    assert stale == []


class TestLeakedSentinel:
    def test_a_price_on_a_page_without_price_view_fails(self, world: World) -> None:
        route = Route(same(expect(OK)), path=lambda world: reverse("demo_price"))
        found = check_route("demo_price", route, world, TRIO)
        assert "GET demo_price as tech_mad: leaks PRICE" in found

    def test_the_same_page_passes_where_the_price_is_allowed(self, world: World) -> None:
        route = Route(
            same(expect(OK)),
            path=lambda world: reverse("demo_price"),
            allowed=frozenset({Sentinel.PRICE}),
        )
        assert check_route("demo_price", route, world, TRIO) == []

    @pytest.mark.parametrize(
        ("sentinel", "rendered"),
        [
            (Sentinel.PRICE, "98765.4321"),
            (Sentinel.PRICE, "98.765,43 €"),
            (Sentinel.PRICE, "98,765.43"),
            (Sentinel.PRICE, "98\u202f765,43"),
            (Sentinel.OTHER_PROJECT_QUANTITY, "4.242,125 m"),
            (Sentinel.SUPPLIER_NAME, "ZYXWVUT SENTINEL SUPPLIES"),
            (Sentinel.SUPPLIER_EMAIL, "mailto:sentinel-supplier@zyxwvut.example"),
            (Sentinel.SUPPLIER_PHONE, "699 00 01 11"),
            (Sentinel.SUPPLIER_PHONE, "+34699000111"),
        ],
    )
    def test_localized_renderings_are_found(self, sentinel: Sentinel, rendered: str) -> None:
        assert sentinel.found_in(f"<td>{rendered}</td>")

    @pytest.mark.parametrize("text", ["198765.43", "98765", "4242", "Sentinel"])
    def test_other_values_are_not(self, text: str) -> None:
        assert not any(sentinel.found_in(text) for sentinel in Sentinel)

    def test_streamed_bodies_are_read(self) -> None:
        response = StreamingHttpResponse(iter([b"a;", Sentinel.SUPPLIER_EMAIL.encode()]))
        assert leaked(response, frozenset()) == [Sentinel.SUPPLIER_EMAIL]


class TestGetViewCallingAService:
    def rows(self, name: str) -> Route:
        return Route(
            same(expect(NOT_ALLOWED), expect(DENIED, mm_mad=OK, mm_sev=NOT_FOUND)),
            path=lambda world: reverse(name, args=[world.org.hospital.pk]),
        )

    def test_a_mutating_view_passes(self, world: World) -> None:
        assert check_route("demo_hold", self.rows("demo_hold"), world, TRIO) == []

    def test_a_view_that_answers_get_fails_and_writes_on_get(self, world: World) -> None:
        route = self.rows("demo_hold_on_get")
        found = check_route("demo_hold_on_get", route, world, ("mm_mad",))
        assert "GET demo_hold_on_get as mm_mad: expected 405, got 200" in found
        # The GET itself put the project on hold.
        assert Project.objects.get(pk=world.org.hospital.pk).status == Project.Status.ON_HOLD


def test_a_system_principal_in_a_view_is_refused(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    """At runtime too: only ``tests`` itself is allowlisted while the suite runs."""
    production: frozenset[str] = principals.CONSTRUCTOR_MODULES["SystemPrincipal"] - {"tests"}
    monkeypatch.setitem(principals.CONSTRUCTOR_MODULES, "SystemPrincipal", production)
    with pytest.raises(PrincipalNotAllowed, match=r"tests\.access\.urls_violations"):
        world.client("mm_mad").post(reverse("demo_worker"))


def test_a_plain_model_admin_fails_the_system_check() -> None:
    registry: dict[type[Model], ModelAdmin[Any]] = admin.site._registry
    original = registry[Location]
    registry[Location] = ModelAdmin(Location, admin.site)
    try:
        assert {error.id for error in checks.check_admin_base_class(None)} == {"gmat.E005"}
    finally:
        registry[Location] = original

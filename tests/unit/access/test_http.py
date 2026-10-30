"""The request side of the engine: ``require`` (S7), view guards, middleware, ``{% can %}``.

Also the admin's permissions through ``CapabilityModelAdmin``.
"""

from collections.abc import Callable
from typing import Any

import pytest
from django.contrib import admin
from django.contrib.auth.models import AnonymousUser
from django.contrib.sessions.backends.db import SessionStore
from django.http import HttpRequest, HttpResponse, HttpResponseBase
from django.template import Context, Template, TemplateSyntaxError
from django.test import Client, RequestFactory, override_settings
from django.urls import path, reverse

from apps.access.admin import CapabilityModelAdmin
from apps.access.context import AccessContext
from apps.access.exceptions import AccessDenied, MethodNotAllowed, NotInScope, StaleSession
from apps.access.middleware import AccessContextMiddleware, access_for, context_for_request
from apps.access.mixins import (
    CapabilityRequiredMixin,
    MutatingViewMixin,
    mutating,
    require,
    require_capability,
)
from apps.access.roles import Capability, Role
from apps.accounts.models import User
from apps.accounts.sessions import SESSION_EPOCH_KEY
from apps.organizations.models import Branch
from tests.org import Org

pytestmark = pytest.mark.django_db

Member = Callable[..., User]
VIEW, MANAGE = Capability.PROJECT_VIEW, Capability.PROJECT_MANAGE


def request_as(user: Any, method: str = "get", path_: str = "/") -> HttpRequest:
    request: HttpRequest = getattr(RequestFactory(), method)(path_)
    request.user = user
    request.session = SessionStore()
    return request


def ok(request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
    return HttpResponse("ok")


def body(response: HttpResponseBase) -> bytes:
    assert isinstance(response, HttpResponse)
    return response.content


# --- a URLconf for the middleware round trips ----------------------------------------


def wrong_method(request: HttpRequest) -> HttpResponse:
    require(request, Capability.CATALOG_VIEW, methods=("POST",))
    return HttpResponse("ok")


def stale(request: HttpRequest) -> HttpResponse:
    raise StaleSession("epoch")


urlpatterns = [
    path("wrong-method/", wrong_method, name="wrong_method"),
    path("stale/", stale, name="stale"),
]


class TestS7Require:
    def test_a_capability_granted_somewhere(self, org: Org, member: Member) -> None:
        request = request_as(member(Role.TECHNICIAN, branch=org.a_mad))
        require(request, Capability.REQUEST_CREATE)
        with pytest.raises(AccessDenied):
            require(request, Capability.PO_VIEW)

    def test_an_object_out_of_scope_is_404(self, org: Org, member: Member) -> None:
        request = request_as(member(Role.MATERIAL_MANAGER, branch=org.a_mad))
        require(request, MANAGE, org.hospital)
        with pytest.raises(NotInScope):
            require(request, MANAGE, org.sev_works)

    def test_an_object_in_scope_without_the_capability_is_403(
        self, org: Org, member: Member
    ) -> None:
        request = request_as(member(Role.WAREHOUSE_OPERATOR, branch=org.a_mad))
        require(request, VIEW, org.hospital, read=VIEW)
        with pytest.raises(AccessDenied):
            require(request, MANAGE, org.hospital, read=VIEW)
        with pytest.raises(NotInScope):
            require(request, MANAGE, org.sev_works, read=VIEW)

    def test_an_unsupported_method_is_405(self, org: Org, member: Member) -> None:
        request = request_as(member(Role.GROUP_ADMIN), "get")
        with pytest.raises(MethodNotAllowed) as excinfo:
            require(request, VIEW, methods=("POST",))
        assert excinfo.value.allowed == ("POST",)
        require(request_as(member(Role.GROUP_ADMIN), "post"), VIEW, methods=("POST",))

    def test_anonymous_is_refused(self) -> None:
        with pytest.raises(AccessDenied):
            require(request_as(AnonymousUser()), Capability.CATALOG_VIEW)


class TestDecorators:
    def test_require_capability(self, org: Org, member: Member) -> None:
        view = require_capability(Capability.PO_VIEW)(ok)
        assert body(view(request_as(member(Role.WAREHOUSE_OPERATOR, branch=org.a_mad)))) == b"ok"
        with pytest.raises(AccessDenied):
            view(request_as(member(Role.TECHNICIAN, branch=org.a_mad)))
        assert (view.required_capability, view.post_only) == (Capability.PO_VIEW, False)  # type: ignore[attr-defined]

    def test_anonymous_visitors_are_sent_to_log_in(self) -> None:
        view = require_capability(Capability.CATALOG_VIEW)(ok)
        response = view(request_as(AnonymousUser(), path_="/things/?page=2"))
        assert response.status_code == 302
        assert response["Location"].startswith(reverse("account_login"))
        assert "next=/things/%3Fpage%3D2" in response["Location"]

    def test_mutating_views_accept_post_only(self, org: Org, member: Member) -> None:
        view = mutating(MANAGE)(ok)
        manager = member(Role.MATERIAL_MANAGER, branch=org.a_mad)
        for method in ("get", "head", "put", "delete"):
            response = view(request_as(manager, method))
            assert response.status_code == 405 and response["Allow"] == "POST"
        assert view(request_as(AnonymousUser(), "get")).status_code == 405  # method first
        assert body(view(request_as(manager, "post"))) == b"ok"
        assert view.post_only is True  # type: ignore[attr-defined]


class Listing(CapabilityRequiredMixin):
    required_capability = Capability.PO_VIEW

    def get(self, request: HttpRequest) -> HttpResponse:
        return HttpResponse("list")


class Approve(MutatingViewMixin):
    required_capability = MANAGE

    def post(self, request: HttpRequest) -> HttpResponse:
        return HttpResponse("done")


class TestMixins:
    def test_capability_required(self, org: Org, member: Member) -> None:
        view = Listing.as_view()
        operator = member(Role.WAREHOUSE_OPERATOR, branch=org.a_mad)
        assert body(view(request_as(operator))) == b"list"
        with pytest.raises(AccessDenied):
            view(request_as(member(Role.TECHNICIAN, branch=org.a_mad)))
        assert view(request_as(AnonymousUser())).status_code == 302

    def test_unsupported_methods_come_first(self, org: Org, member: Member) -> None:
        class ReadOnly(Listing):
            http_method_names = ["get"]

        assert ReadOnly.as_view()(request_as(AnonymousUser(), "post")).status_code == 405

    def test_mutating_views_are_post_only(self, org: Org, member: Member) -> None:
        view = Approve.as_view()
        manager = member(Role.MATERIAL_MANAGER, branch=org.a_mad)
        assert view(request_as(manager, "get")).status_code == 405
        assert body(view(request_as(manager, "post"))) == b"done"
        with pytest.raises(AccessDenied):
            view(request_as(member(Role.TECHNICIAN, branch=org.a_mad), "post"))


class TestMiddleware:
    def test_sets_the_requests_access_context(self, org: Org, member: Member) -> None:
        manager = member(Role.MATERIAL_MANAGER, branch=org.a_mad)
        request = request_as(manager)
        request.session[SESSION_EPOCH_KEY] = 0
        seen: list[AccessContext] = []

        def view(request: HttpRequest) -> HttpResponse:
            seen.append(request.access)  # type: ignore[attr-defined]
            return HttpResponse()

        AccessContextMiddleware(view)(request)
        (ctx,) = seen
        assert ctx.user == manager and ctx.has(MANAGE)
        assert ctx.principal.session_epoch == 0 and ctx.principal.user_id == manager.pk
        assert access_for(request) is ctx

    def test_anonymous_requests_get_an_anonymous_context(self) -> None:
        ctx = context_for_request(request_as(AnonymousUser()))
        assert not ctx.is_authenticated
        with pytest.raises(ValueError, match="no session"):
            _ = ctx.principal
        assert ctx.mfa_passed is False

    def test_mfa_passed_reads_this_sessions_records(
        self, client: Client, member: Member, frozen_totp: int
    ) -> None:
        from tests.support import enroll_totp, mfa_login

        admin = member(Role.GROUP_ADMIN)
        client.force_login(admin)
        request = request_as(admin)
        request.session = client.session
        assert context_for_request(request).mfa_passed is False
        client.logout()
        mfa_login(client, admin.email, enroll_totp(admin))
        request.session = client.session
        assert context_for_request(request).mfa_passed is True

    def test_access_for_builds_it_where_the_middleware_did_not_run(self, member: Member) -> None:
        request = request_as(member(Role.GROUP_ADMIN))
        ctx = access_for(request)
        assert access_for(request) is ctx and ctx.has(Capability.ORG_MANAGE)

    def test_a_context_for_a_user_alone_has_no_session(self, member: Member) -> None:
        ctx = AccessContext.for_user(member(Role.GROUP_ADMIN))
        assert ctx.mfa_passed is False
        assert ctx.can(Capability.ORG_MANAGE, Branch.objects.first() or Branch()) is False

    @override_settings(ROOT_URLCONF=__name__)
    def test_method_not_allowed_becomes_a_405_page(
        self, client: Client, make_user: Callable[..., User]
    ) -> None:
        client.force_login(make_user())
        response = client.get("/wrong-method/")
        assert response.status_code == 405
        assert response["Allow"] == "POST"

    @override_settings(ROOT_URLCONF=__name__)
    def test_a_stale_session_is_flushed(
        self, client: Client, make_user: Callable[..., User]
    ) -> None:
        client.force_login(make_user())
        assert client.get("/stale/").status_code == 403
        assert "_auth_user_id" not in client.session

    def test_other_errors_pass_through(self) -> None:
        middleware = AccessContextMiddleware(ok)
        assert middleware.process_exception(request_as(AnonymousUser()), ValueError()) is None


class TestCanTag:
    def render(self, text: str, **context: Any) -> str:
        return Template("{% load access %}" + text).render(Context(context))

    def test_without_an_object(self, org: Org, member: Member) -> None:
        text = '{% can "price.view" %}price{% else %}hidden{% endcan %}'
        manager = request_as(member(Role.MATERIAL_MANAGER, branch=org.a_mad))
        operator = request_as(member(Role.WAREHOUSE_OPERATOR, branch=org.a_mad))
        assert self.render(text, request=manager) == "price"
        assert self.render(text, request=operator) == "hidden"
        assert self.render('{% can "price.view" %}price{% endcan %}', request=operator) == ""

    def test_with_an_object(self, org: Org, member: Member) -> None:
        text = '{% can "project.manage" project %}edit{% else %}-{% endcan %}'
        request = request_as(member(Role.MATERIAL_MANAGER, branch=org.a_mad))
        assert self.render(text, request=request, project=org.hospital) == "edit"
        assert self.render(text, request=request, project=org.sev_works) == "-"
        assert self.render(text, request=request, project="not a model") == "-"

    def test_without_a_request_nothing_is_shown(self) -> None:
        assert self.render('{% can "catalog.view" %}x{% else %}none{% endcan %}') == "none"

    def test_usage(self) -> None:
        with pytest.raises(TemplateSyntaxError, match="Usage"):
            Template("{% load access %}{% can %}x{% endcan %}")
        with pytest.raises(TemplateSyntaxError, match="Usage"):
            Template('{% load access %}{% can "a" b c %}x{% endcan %}')


class TestCapabilityModelAdmin:
    class BranchEditor(CapabilityModelAdmin):
        edit_capability = MANAGE

    def test_edits_need_the_capability_within_scope(self, org: Org, member: Member) -> None:
        editor = self.BranchEditor(Branch, admin.site)
        manager = request_as(member(Role.MATERIAL_MANAGER, branch=org.a_mad))
        technician = request_as(member(Role.TECHNICIAN, branch=org.a_mad))
        assert editor.may_edit(manager) is True
        assert editor.may_edit(manager, org.a_mad) is True
        assert editor.may_edit(manager, org.a_sev) is False
        assert editor.may_edit(technician) is False

    def test_an_unscoped_model_needs_the_capability_only(self, member: Member) -> None:
        editor = self.BranchEditor(User, admin.site)
        admin_user = member(Role.GROUP_ADMIN)
        assert editor.may_edit(request_as(admin_user), admin_user) is True

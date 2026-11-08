"""The display preferences cookie and ``/ui/preferences/`` (M8b.2, Q69).

Each control of the plan's "Preference cookie security" list has a test here: attributes, written
only by the server, content, never reflected, never decides anything else, the endpoint.
"""

import re
from pathlib import Path
from typing import Any

import pytest
from django.conf import settings
from django.test import Client, RequestFactory
from django.urls import reverse

from apps.core.context_processors import preferences
from apps.core.preferences import CATEGORY_KEYS, MAX_COOKIE_BYTES, Preferences, parse
from tests.org import Org, People, client_for

ROOT = Path(__file__).resolve().parents[3]
URL = "/ui/preferences/"
NAME = settings.UI_COOKIE_NAME

HOSTILE = [
    '"><script>alert(1)</script>',
    "dark\"'<b>x</b>",
    "dark;hidden",
    "dark\r\nSet-Cookie: pwned=1",
    "x" * 1_000_000,
    "dark.dark",
    "dark.light",
    "DARK",
    "dark.f-nowhere",
    "f-work.f-work",
    "",
    ".",
    "dark..hidden",
]


def root_tag(html: str) -> str:
    found = re.search(r"<html[^>]*>", html)
    assert found
    return found.group(0)


def post(client: Client, data: dict[str, str], *, htmx: bool = False) -> Any:
    return client.post(URL, data, headers={"HX-Request": "true"} if htmx else {})


def cookie_value(client: Client) -> str | None:
    morsel = client.cookies.get(NAME)
    return morsel.value if morsel else None


class TestGrammar:
    def test_the_defaults(self) -> None:
        assert parse(None) == Preferences()
        assert Preferences().serialize() == ""
        assert Preferences().theme == "" and not Preferences().menu_hidden

    def test_every_allowed_state_round_trips(self) -> None:
        everything = Preferences(theme="dark", menu_hidden=True, folded=frozenset(CATEGORY_KEYS))
        value = everything.serialize()
        assert value == "dark.hidden." + ".".join(f"f-{key}" for key in CATEGORY_KEYS)
        assert parse(value) == everything
        assert len(value.encode()) <= MAX_COOKIE_BYTES
        assert parse("light") == Preferences(theme="light")
        assert parse("f-site.f-work").folded == {"site", "work"}

    @pytest.mark.parametrize("raw", HOSTILE)
    def test_anything_else_is_ignored_as_a_whole(self, raw: str) -> None:
        assert parse(raw) == Preferences()

    def test_a_value_that_is_not_text_is_ignored(self) -> None:
        assert parse(b"dark") == Preferences()
        assert parse(7) == Preferences()


class TestTheEndpoint:
    def test_get_and_head_are_refused(self, client: Client) -> None:
        assert client.get(URL).status_code == 405
        assert client.head(URL).status_code == 405
        assert NAME not in client.cookies

    def test_a_choice_answers_303_back_and_sets_the_cookie(self, client: Client) -> None:
        response = post(client, {"theme": "dark", "next": "/accounts/login/"})
        assert response.status_code == 303
        assert response["Location"] == "/accounts/login/"
        assert cookie_value(client) == "dark"

    def test_the_cookie_is_host_prefixed_secure_and_not_readable_by_scripts(
        self, client: Client
    ) -> None:
        post(client, {"theme": "dark"})
        morsel = client.cookies[NAME]
        assert NAME == "__Host-ui"
        assert morsel["secure"]
        assert morsel["httponly"]
        assert morsel["samesite"] == "Lax"
        assert morsel["path"] == "/"
        assert not morsel["domain"]
        assert morsel["max-age"] == 365 * 24 * 3600

    def test_the_choices_add_up_and_system_removes_the_theme(self, client: Client) -> None:
        post(client, {"theme": "light"})
        post(client, {"menu": "hidden"})
        post(client, {"fold": "insights", "state": "closed"})
        post(client, {"fold": "work", "state": "closed"})
        assert cookie_value(client) == "light.hidden.f-work.f-insights"
        post(client, {"theme": "system"})
        assert cookie_value(client) == "hidden.f-work.f-insights"
        post(client, {"fold": "work", "state": "open"})
        post(client, {"menu": "shown"})
        post(client, {"fold": "insights", "state": "open"})
        assert cookie_value(client) == ""  # nothing is left: the cookie is expired
        assert client.cookies[NAME]["max-age"] == 0

    def test_an_empty_post_changes_nothing_and_sets_no_cookie(self, client: Client) -> None:
        response = post(client, {})
        assert response.status_code == 303
        assert "Set-Cookie" not in response

    @pytest.mark.parametrize(
        "data",
        [
            {"theme": "purple"},
            {"theme": "Dark"},
            {"menu": "maybe"},
            {"fold": "nowhere", "state": "open"},
            {"fold": "work"},
            {"state": "open"},
            {"fold": "work", "state": "sideways"},
            {"theme": "dark", "menu": "x"},
            {"theme": "x" * 10_000},
        ],
    )
    def test_a_value_outside_the_allowlist_is_a_400_and_the_cookie_stays(
        self, client: Client, data: dict[str, str]
    ) -> None:
        post(client, {"theme": "light"})
        response = post(client, data)
        assert response.status_code == 400
        assert "Set-Cookie" not in response
        assert cookie_value(client) == "light"

    @pytest.mark.parametrize(
        "target",
        [
            "https://evil.example/",
            "//evil.example/",
            "javascript:alert(1)",
            "/ok\r\nSet-Cookie: x=1",
            "/" + "a" * 3000,
            "\\\\evil.example",
        ],
    )
    def test_a_foreign_or_malformed_next_falls_back_to_the_home_page(
        self, client: Client, target: str
    ) -> None:
        response = post(client, {"theme": "dark", "next": target})
        assert response.status_code == 303
        assert response["Location"] == "/"

    def test_a_local_next_is_kept(self, client: Client) -> None:
        response = post(client, {"menu": "hidden", "next": "/requests/?page=2"})
        assert response["Location"] == "/requests/?page=2"

    def test_an_htmx_request_gets_204_and_no_redirect(self, client: Client) -> None:
        response = post(client, {"theme": "dark", "next": "/x/"}, htmx=True)
        assert response.status_code == 204
        assert "Location" not in response
        assert cookie_value(client) == "dark"

    def test_it_needs_the_csrf_token_and_a_refusal_sets_no_cookie(self) -> None:
        strict = Client(enforce_csrf_checks=True)
        response = strict.post(URL, {"theme": "dark"})
        assert response.status_code == 403
        assert NAME not in strict.cookies

    def test_the_form_carries_the_token_and_it_works(self, client: Client) -> None:
        strict = Client(enforce_csrf_checks=True)
        html = strict.get(reverse("account_login")).content.decode()
        token = re.search(r'name="csrfmiddlewaretoken" value="([^"]+)"', html)
        assert token
        response = strict.post(URL, {"theme": "dark", "csrfmiddlewaretoken": token.group(1)})
        assert response.status_code == 303

    def test_answers_are_never_cached(self, client: Client) -> None:
        response = post(client, {"theme": "dark"})
        assert "no-store" in response["Cache-Control"]

    def test_it_needs_no_sign_in_and_a_signed_in_user_may_use_it(
        self, org: Org, people: People
    ) -> None:
        client = client_for(people.tech_mad)
        assert post(client, {"theme": "light"}).status_code == 303
        assert cookie_value(client) == "light"

    def test_a_privileged_session_without_mfa_may_still_change_the_theme(
        self, org: Org, people: People
    ) -> None:
        client = client_for(people.group_admin, mfa=False)
        assert post(client, {"theme": "dark"}).status_code == 303

    def test_it_writes_no_audit_event(self, client: Client, db: None) -> None:
        from apps.audit.models import AuditEvent

        before = AuditEvent.objects.count()
        post(client, {"theme": "dark"})
        assert AuditEvent.objects.count() == before


class TestTheCookieDrivesThePage:
    def render(self, client: Client, cookie: str | None) -> str:
        if cookie is not None:
            client.cookies[NAME] = cookie
        return client.get(reverse("account_login")).content.decode()

    @pytest.mark.parametrize(
        ("cookie", "theme", "scheme"),
        [
            (None, None, "light dark"),
            ("", None, "light dark"),
            ("light", "light", "light"),
            ("dark", "dark", "dark"),
            ("hidden.f-work", None, "light dark"),
        ],
    )
    def test_each_theme_is_written_on_the_root_element(
        self, client: Client, cookie: str | None, theme: str | None, scheme: str
    ) -> None:
        html = self.render(client, cookie)
        root = re.search(r"<html[^>]*>", html)
        assert root
        if theme is None:
            assert "data-theme" not in root.group(0)
        else:
            assert f'data-theme="{theme}"' in root.group(0)
        assert f'content="{scheme}"' in html

    def test_the_entrance_pages_carry_the_theme_switch_with_the_state_pressed(
        self, client: Client
    ) -> None:
        html = self.render(client, "dark")
        assert 'role="group"' in html
        pressed = re.findall(r'value="(\w+)"\s+aria-pressed="(\w+)"', html)
        assert pressed == [("system", "false"), ("light", "false"), ("dark", "true")]
        assert re.findall(r'value="(\w+)"\s+aria-pressed="(\w+)"', self.render(Client(), None))[
            0
        ] == ("system", "true")

    @pytest.mark.parametrize("raw", [raw for raw in HOSTILE if ";" not in raw])
    def test_a_hostile_value_is_ignored_and_never_reflected(self, client: Client, raw: str) -> None:
        plain = Client().get(reverse("account_login"))
        request = RequestFactory().get("/", headers={"cookie": "x=1"})
        request.COOKIES[NAME] = raw
        assert preferences(request) == {
            "ui_theme": "",
            "ui_menu_hidden": False,
            "ui_folded": frozenset(),
        }
        response = client.get(reverse("account_login"), HTTP_COOKIE=f"{NAME}={raw[:8000]}")
        assert response.status_code == 200
        body = response.content.decode()
        sample = re.sub(r"[^\w<>\"';]", "", raw)[:30]
        if sample and sample not in ("dark", "light"):
            assert sample not in body
        assert "data-theme=" not in root_tag(body)
        assert "pwned" not in str(response.headers)
        assert response["Content-Security-Policy"] == plain["Content-Security-Policy"]

    def test_non_utf8_bytes_and_two_cookies_of_the_same_name_do_no_harm(
        self, client: Client
    ) -> None:
        for raw in (f"{NAME}=\xff\xfe", f"{NAME}=dark; {NAME}=light", f"{NAME}=dark; {NAME}=\xff"):
            response = client.get(reverse("account_login"), HTTP_COOKIE=raw)
            assert response.status_code == 200

    def test_a_megabyte_cookie_is_ignored(self, client: Client) -> None:
        client.cookies[NAME] = "dark" * 250_000
        response = client.get(reverse("account_login"))
        assert response.status_code == 200
        assert "data-theme=" not in root_tag(response.content.decode())


def main_of(html: str) -> str:
    found = re.search(r"<main id=\"main\".*?</main>", html, re.S)
    assert found
    return found.group(0)


class TestItDecidesNothingElse:
    def test_the_same_request_with_and_without_the_cookie_gets_the_same_data(
        self, org: Org, people: People
    ) -> None:
        for who, url in (
            ("tech_mad", "/requests/"),
            ("mm_mad", "/purchasing/orders/"),
            ("wo_mad", "/receiving/"),
            ("partner", "/projects/"),
        ):
            plain_client = client_for(getattr(people, who))
            cooked = client_for(getattr(people, who))
            cooked.cookies[NAME] = "dark.hidden.f-work.f-purchasing.f-organization"
            plain = plain_client.get(url)
            other = cooked.get(url)
            assert plain.status_code == other.status_code == 200
            assert main_of(plain.content.decode()) == main_of(other.content.decode())

    def test_a_forged_cookie_does_not_open_a_page_or_a_scope(
        self, org: Org, people: People
    ) -> None:
        client = client_for(people.tech_mad)
        client.cookies[NAME] = "dark.hidden"
        assert client.get("/suppliers/").status_code == 403
        assert client.get("/audit/").status_code == 403
        assert client.get("/purchasing/").status_code == 403

    def test_the_cookie_changes_only_the_theme_the_menu_and_the_folded_categories(
        self, org: Org, people: People
    ) -> None:
        plain = client_for(people.mm_mad).get("/").content.decode()
        cooked_client = client_for(people.mm_mad)
        cooked_client.cookies[NAME] = "dark.hidden.f-work"
        cooked = cooked_client.get("/").content.decode()
        token = re.compile(r'(csrfmiddlewaretoken" value="|X-CSRFToken": ")[^"]+')

        def strip(html: str) -> str:
            html = re.sub(r"<html[^>]*>", "<html>", html)
            html = re.sub(r'<meta name="color-scheme"\s+content="[^"]*">', "", html)
            html = re.sub(r'<nav id="app-menu".*?</nav>', "", html, flags=re.S)
            html = re.sub(r"<form action=\"/ui/preferences/\".*?</form>", "", html, flags=re.S)
            html = re.sub(r"<div id=\"(more|user)-menu\".*?\n    </div>", "", html, flags=re.S)
            return token.sub(r"\1", html)

        assert strip(plain) == strip(cooked)


class TestNothingWritesTheCookieButTheServer:
    def test_no_script_touches_document_cookie(self) -> None:
        for script in (ROOT / "static" / "js").glob("*.js"):
            assert "document.cookie" not in script.read_text("utf-8"), script.name

    def test_no_other_view_sets_the_cookie(self, client: Client) -> None:
        for url in ("/", "/accounts/login/", "/healthz/"):
            response = client.get(url)
            assert NAME not in response.cookies, url
        assert NAME not in client.post(reverse("set_language"), {"language": "en"}).cookies

    def test_sign_out_keeps_it_like_the_language_cookie(self, org: Org, people: People) -> None:
        client = client_for(people.tech_mad)
        post(client, {"theme": "dark"})
        response = client.post(reverse("account_logout"))
        assert NAME not in response.cookies  # not deleted, not rewritten
        assert cookie_value(client) == "dark"

    def test_the_cookie_header_is_scrubbed_from_error_reports(self) -> None:
        from config.logging import scrub_event

        secret = "dark.hidden"
        event = {"request": {"cookies": {NAME: secret}, "headers": {"Cookie": f"{NAME}={secret}"}}}
        assert secret not in str(scrub_event(event, hint=None))

    def test_the_logout_cookie_policy_is_documented(self) -> None:
        security = (ROOT / "docs" / "security.md").read_text("utf-8")
        assert "__Host-ui" in security

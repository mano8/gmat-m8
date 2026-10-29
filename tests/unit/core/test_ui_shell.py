"""The UI shell (step M1.6): layouts, components, error pages, languages, vendored htmx."""

import hashlib
import json
import os
import re
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest
from django.conf import settings
from django.contrib.messages.storage.fallback import FallbackStorage
from django.core.management import call_command
from django.core.signals import setting_changed
from django.http import HttpRequest, HttpResponse
from django.template.loader import render_to_string
from django.test import Client, RequestFactory, override_settings
from django.urls import reverse

from apps.accounts.models import User
from apps.core.context_processors import ui
from apps.core.middleware import ErrorPagesMiddleware

ROOT = Path(__file__).resolve().parents[3]
HTMX_SHA256 = "d6fdc75f204e6bdefa99b69bf1e6d4ac69b8a364f77929f45c13476b4000f717"

INLINE_SCRIPT = re.compile(r"<script(?![^>]*\bsrc=)(?![^>]*type=\"application/json\")[^>]*>", re.I)
INLINE_STYLE = re.compile(r"(<style\b|\sstyle\s*=)", re.I)
INLINE_HANDLER = re.compile(r"\son[a-z]+\s*=", re.I)


def assert_csp_clean(html: str) -> None:
    """No inline script, style or event handler: the CSP forbids all three."""
    assert not INLINE_SCRIPT.search(html)
    assert not INLINE_STYLE.search(html)
    assert not INLINE_HANDLER.search(html)
    assert "javascript:" not in html.lower()


@pytest.fixture(scope="module")
def compiled_locale() -> Iterator[None]:
    """Compile the catalogs (the .mo files are not committed); skip without gettext.

    CI installs gettext, so there a missing tool is a failure rather than a silent skip.
    """
    try:
        call_command("compilemessages", verbosity=0)
    except Exception as error:
        if os.environ.get("CI"):
            raise
        pytest.skip(f"gettext tools are not available: {error}")
    # Earlier tests may have cached empty catalogs; Django's LOCALE_PATHS handler drops them.
    setting_changed.send(
        sender=None, setting="LOCALE_PATHS", value=settings.LOCALE_PATHS, enter=False
    )
    yield


def request_with_messages(*texts: tuple[int, str]) -> HttpRequest:
    request = RequestFactory().get("/")
    request.session = {}  # type: ignore[assignment]
    storage = FallbackStorage(request)
    request._messages = storage  # type: ignore[attr-defined]
    for level, text in texts:
        storage.add(level, text)
    return request


class TestVendoredHtmx:
    def test_the_file_is_the_recorded_release(self) -> None:
        data = (ROOT / "static" / "vendor" / "htmx.min.js").read_bytes()
        assert hashlib.sha256(data).hexdigest() == HTMX_SHA256

    def test_the_record_names_the_same_checksum(self) -> None:
        assert HTMX_SHA256 in (ROOT / "static" / "vendor" / "README.md").read_text("utf-8")


class TestBaseLayout:
    def test_htmx_is_configured_by_meta_tag_with_no_history_cache(self, client: Client) -> None:
        html = client.get(reverse("account_login")).content.decode()
        meta = re.search(r"<meta name=\"htmx-config\"\s+content='([^']+)'", html)
        assert meta, "the htmx-config meta tag is missing"
        config = json.loads(meta.group(1))
        assert config["historyCacheSize"] == 0
        assert config["refreshOnHistoryMiss"] is True
        assert config["allowEval"] is False
        assert config["allowScriptTags"] is False
        assert config["includeIndicatorStyles"] is False
        assert config["selfRequestsOnly"] is True

    def test_the_csrf_token_travels_in_hx_headers_on_body(self, client: Client) -> None:
        html = client.get(reverse("account_login")).content.decode()
        body = re.search(r"<body[^>]*hx-headers='([^']+)'", html)
        assert body
        token = json.loads(body.group(1))["X-CSRFToken"]
        assert len(token) >= 32

    def test_scripts_and_styles_are_files_never_inline(
        self, client: Client, make_user: Callable[..., User]
    ) -> None:
        login = client.get(reverse("account_login")).content.decode()
        assert_csp_clean(login)
        assert 'src="/static/vendor/htmx.min.js"' in login
        assert 'href="/static/dist/app.css"' in login
        client.force_login(make_user())
        home = client.get(reverse("core:home")).content.decode()
        assert_csp_clean(home)

    def test_the_viewport_allows_zoom(self, client: Client) -> None:
        html = client.get(reverse("account_login")).content.decode()
        assert 'content="width=device-width, initial-scale=1"' in html
        assert "user-scalable" not in html
        assert "maximum-scale" not in html


class TestAppShell:
    def test_signed_in_pages_have_both_navigations_and_the_active_entry(
        self, client: Client, make_user: Callable[..., User], compiled_locale: None
    ) -> None:
        client.force_login(make_user())
        html = client.get(reverse("core:home")).content.decode()
        assert html.count('aria-label="Navegación principal"') == 2
        assert html.count('aria-current="page"') == 2  # sidebar and bottom bar
        assert 'action="/accounts/logout/"' in html
        assert 'id="main"' in html
        assert 'href="#main"' in html  # skip link

    def test_sign_out_is_a_post_form_never_a_link(
        self, client: Client, make_user: Callable[..., User]
    ) -> None:
        client.force_login(make_user())
        html = client.get(reverse("core:home")).content.decode()
        assert not re.search(r"<a [^>]*href=\"/accounts/logout/\"", html)

    def test_anonymous_pages_have_no_app_navigation(
        self, client: Client, compiled_locale: None
    ) -> None:
        html = client.get(reverse("account_login")).content.decode()
        assert "Navegación principal" not in html
        assert 'id="language"' in html

    def test_the_login_page_does_not_offer_sign_up(self, client: Client) -> None:
        html = client.get(reverse("account_login")).content.decode()
        assert "signup" not in html.lower()
        assert 'name="login"' in html

    def test_allauth_forms_use_the_project_styles(self, client: Client) -> None:
        html = client.get(reverse("account_reset_password")).content.decode()
        assert 'class="field' in html
        assert "btn btn-primary" in html


class TestMessages:
    def test_success_fades_and_errors_stay(self) -> None:
        request = request_with_messages((25, "Saved"), (40, "Failed"))
        html = render_to_string("components/messages.html", request=request)
        assert "toast-success" in html
        assert 'data-autohide="6000"' in html
        assert 'role="alert"' in html  # the error
        assert html.count("data-dismiss") == 2
        assert_csp_clean(html)

    def test_message_text_is_escaped(self) -> None:
        request = request_with_messages((25, "<b>bold</b>"))
        html = render_to_string("components/messages.html", request=request)
        assert "<b>bold</b>" not in html
        assert "&lt;b&gt;" in html

    def test_no_messages_no_markup(self) -> None:
        html = render_to_string("components/messages.html", request=request_with_messages())
        assert "toast" not in html


class TestComponents:
    def test_button_variants(self) -> None:
        primary = render_to_string(
            "components/button.html", {"label": "Save", "variant": "primary", "type": "submit"}
        )
        assert 'type="submit"' in primary
        assert "btn-primary" in primary
        link = render_to_string("components/button.html", {"label": "Open", "href": "/x/"})
        assert link.strip().startswith("<a ")
        assert 'href="/x/"' in link
        block = render_to_string("components/button.html", {"label": "Go", "block": True})
        assert "btn-block" in block
        assert 'type="button"' in block

    def test_button_label_is_escaped(self) -> None:
        html = render_to_string("components/button.html", {"label": "<script>x</script>"})
        assert "<script>" not in html

    def test_card_empty_state_and_icon(self) -> None:
        card = render_to_string("components/card.html", {"title": "Title", "text": "Body"})
        assert "Title" in card
        assert "Body" in card
        empty = render_to_string(
            "components/empty_state.html",
            {"title": "Nothing", "text": "Add one", "href": "/new/", "label": "New"},
        )
        assert 'href="/new/"' in empty
        assert "btn-primary" in empty
        for name in ("home", "shield", "logout", "check", "alert"):
            svg = render_to_string("components/icon.html", {"name": name})
            assert 'aria-hidden="true"' in svg
            assert "<path" in svg

    def test_table_cells_carry_their_column_label(self) -> None:
        html = render_to_string(
            "components/table.html",
            {"columns": ["Item", "Qty"], "rows": [["Cable", "5"], ["Plug <b>", "2"]]},
        )
        assert html.count("<tr>") == 3  # header + two rows
        assert 'data-label="Item"' in html
        assert 'data-label="Qty"' in html
        assert "&lt;b&gt;" in html

    def test_pagination_only_for_several_pages(self) -> None:
        from django.core.paginator import Paginator

        single = Paginator(range(5), 10).page(1)
        assert "<nav" not in render_to_string("components/pagination.html", {"page_obj": single})
        paginator = Paginator(range(25), 10)
        middle = render_to_string(
            "components/pagination.html",
            {"page_obj": paginator.page(2), "extra_query": "&q=cable"},
        )
        assert 'href="?page=1&amp;q=cable"' in middle
        assert 'href="?page=3&amp;q=cable"' in middle
        first = render_to_string("components/pagination.html", {"page_obj": paginator.page(1)})
        assert 'rel="prev"' not in first
        assert 'aria-disabled="true"' in first

    def test_field_group_shows_label_help_and_errors(self) -> None:
        from django import forms

        class Form(forms.Form):
            qty = forms.DecimalField(label="Quantity", help_text="In units")

        form = Form(data={"qty": "x"})
        assert not form.is_valid()
        html = render_to_string("components/field.html", {"field": form["qty"]})
        assert "Quantity" in html
        assert "In units" in html
        assert "field-error" in html
        assert "errorlist" in html

    def test_forms_render_through_the_project_templates(self) -> None:
        from django import forms

        class Form(forms.Form):
            name = forms.CharField(label="Name")

        html = Form().as_div()
        assert 'class="field"' in html


class TestErrorPages:
    @pytest.mark.parametrize(
        ("status", "needle"),
        [(400, "400"), (403, "403"), (404, "404"), (405, "405"), (409, "409"), (429, "429")],
    )
    def test_bare_error_responses_become_pages(self, status: int, needle: str) -> None:
        def view(request: HttpRequest) -> HttpResponse:
            response = HttpResponse(status=status)
            response["Allow"] = "POST"
            response["Retry-After"] = "30"
            return response

        request = RequestFactory().get("/anything/?secret=1")
        request.user = None  # type: ignore[assignment]
        page = ErrorPagesMiddleware(view)(request)
        if status in {405, 409, 429}:
            body = page.content.decode()  # type: ignore[attr-defined]
            assert needle in body
            assert page.status_code == status
            assert page["Allow"] == "POST"
            assert page["Retry-After"] == "30"
            assert "secret" not in body
            assert "/anything/" not in body
            assert_csp_clean(body)
        else:
            assert page.status_code == status  # handler pages cover these (see below)

    def test_a_response_with_a_body_is_left_alone(self) -> None:
        original = HttpResponse("details", status=409)
        request = RequestFactory().get("/")
        assert ErrorPagesMiddleware(lambda r: original)(request) is original

    def test_the_405_of_a_post_only_view_is_a_page(
        self, client: Client, make_user: Callable[..., User]
    ) -> None:
        client.force_login(make_user())
        response = client.post(reverse("core:home"))
        assert response.status_code == 405
        assert "405" in response.content.decode()
        assert "no-store" in response["Cache-Control"]

    def test_404_shows_no_internals(self, client: Client) -> None:
        response = client.get("/accounts/secret-looking-path/?token=abc")
        assert response.status_code == 404
        html = response.content.decode()
        assert "secret-looking-path" not in html
        assert "token=abc" not in html
        assert "Traceback" not in html
        assert_csp_clean(html)

    def test_403_csrf_failure_is_a_page(self) -> None:
        strict = Client(enforce_csrf_checks=True)
        response = strict.post(reverse("account_login"), {"login": "a", "password": "b"})
        assert response.status_code == 403
        html = response.content.decode()
        assert "403" in html
        assert 'class="btn btn-primary"' in html  # our page, not Django's built-in one
        assert "Referer checking" not in html and "Origin checking" not in html
        assert "DEBUG" not in html

    def test_500_renders_without_a_request_or_a_user(self, client: Client) -> None:
        client.raise_request_exception = False
        with override_settings(ROOT_URLCONF="tests.unit.config.urls_raise", DEBUG=False):
            response = client.get("/boom/abc/")
        assert response.status_code == 500
        html = response.content.decode()
        assert "500" in html
        assert "RuntimeError" not in html
        assert "/boom/" not in html
        assert_csp_clean(html)

    def test_cache_unavailable_uses_the_429_page(self, client: Client) -> None:
        from apps.core.cache import CacheUnavailable
        from apps.core.middleware import CacheUnavailableMiddleware

        request = RequestFactory().get("/")
        request.user = None  # type: ignore[assignment]
        response = CacheUnavailableMiddleware(lambda r: HttpResponse()).process_exception(
            request, CacheUnavailable("full")
        )
        assert response is not None
        assert response.status_code == 429
        assert "429" in response.content.decode()


@pytest.mark.usefixtures("compiled_locale")
class TestLanguages:
    def test_spanish_is_the_default(self, client: Client, make_user: Callable[..., User]) -> None:
        client.force_login(make_user())
        response = client.get(reverse("core:home"))
        assert '<html lang="es"' in response.content.decode()
        assert "Inicio" in response.content.decode()

    def test_the_switcher_sets_a_host_cookie_and_changes_the_page(
        self, client: Client, make_user: Callable[..., User]
    ) -> None:
        client.force_login(make_user())
        switched = client.post(reverse("set_language"), {"language": "fr", "next": "/"})
        assert switched.status_code == 302
        cookie = switched.cookies["__Host-language"]
        assert cookie["secure"]
        assert cookie["httponly"]
        assert cookie["samesite"] == "Lax"
        assert cookie["path"] == "/"
        assert not cookie["domain"]
        page = client.get(reverse("core:home")).content.decode()
        assert '<html lang="fr"' in page
        assert "Accueil" in page
        assert "Se déconnecter" in page

    def test_english_uses_the_source_strings(
        self, client: Client, make_user: Callable[..., User]
    ) -> None:
        client.force_login(make_user())
        client.post(reverse("set_language"), {"language": "en", "next": "/"})
        page = client.get(reverse("core:home")).content.decode()
        assert '<html lang="en"' in page
        assert "Sign out" in page
        assert "Security" in page

    def test_an_unknown_language_is_ignored(self, client: Client) -> None:
        client.post(reverse("set_language"), {"language": "de", "next": "/"})
        assert '<html lang="es"' in client.get(reverse("account_login")).content.decode()

    def test_the_switcher_refuses_an_off_site_redirect(self, client: Client) -> None:
        response = client.post(
            reverse("set_language"), {"language": "en", "next": "https://evil.example/"}
        )
        assert response.status_code == 302
        assert response["Location"] == "/"

    def test_every_catalog_translates_every_shell_string(self) -> None:
        from django.utils import translation

        for code in ("es", "fr"):
            with translation.override(code):
                for text in ("Home", "Sign out", "Security", "Language", "Page not found"):
                    assert translation.gettext(text) != text, f"{code}: {text}"


class TestLanguageSwitcherTarget:
    def test_returns_to_the_current_page(self) -> None:
        request = RequestFactory().get("/accounts/login/")
        assert ui(request) == {"language_next": "/accounts/login/"}

    def test_a_token_path_never_ends_up_in_the_form(self) -> None:
        request = RequestFactory().get("/accounts/password/reset/key/MQ-0123456789abcdef/")
        assert ui(request) == {"language_next": "/"}

    def test_the_page_carries_it_in_a_hidden_field(self, client: Client) -> None:
        html = client.get(reverse("account_login")).content.decode()
        assert '<input type="hidden" name="next" value="/accounts/login/">' in html


class TestStaticFiles:
    def test_collectstatic_hashes_assets_and_leaves_out_the_tailwind_input(
        self, tmp_path: Path
    ) -> None:
        storage = {
            "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
            "staticfiles": {"BACKEND": "whitenoise.storage.CompressedManifestStaticFilesStorage"},
        }
        with override_settings(STORAGES=storage, STATIC_ROOT=str(tmp_path)):
            call_command("collectstatic", interactive=False, verbosity=0)
        names: list[Any] = sorted(p.relative_to(tmp_path).as_posix() for p in tmp_path.rglob("*"))
        assert any(re.fullmatch(r"vendor/htmx.min.[0-9a-f]{12}.js", n) for n in names), names
        assert any(re.fullmatch(r"js/app\.[0-9a-f]{12}\.js", n) for n in names), names
        assert not [n for n in names if n.startswith("src")], "the Tailwind input must not ship"
        assert "vendor/README.md" not in names
        assert "staticfiles.json" in names

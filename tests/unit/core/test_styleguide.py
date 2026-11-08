"""``manage styleguide`` (ADR-0027, M8b.1): every component drawn, for review."""

import re
from pathlib import Path

import pytest
from django.conf import settings
from django.core.management import CommandError, call_command

from tests.unit.core.test_ui_shell import assert_csp_clean

COMPONENTS = settings.BASE_DIR / "templates" / "components"


class Settings:
    """What the command reads of ``django.conf.settings``: a checkout with a built stylesheet."""

    def __init__(self, base: Path) -> None:
        self.BASE_DIR = base


@pytest.fixture
def checkout(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A stand-in for the repository's ``static/`` (CI has not built the CSS), so the command
    runs the same everywhere; the real build is checked by ``tests/e2e/test_styleguide.py``."""
    base = tmp_path / "repo"
    for name, file in (("dist", "app.css"), ("vendor/fonts", "a.woff2"), ("js", "app.js")):
        (base / "static" / name).mkdir(parents=True)
        (base / "static" / name / file).write_text("x", encoding="utf-8")
    from apps.core.management.commands import styleguide

    monkeypatch.setattr(styleguide, "settings", Settings(base))
    return base


@pytest.fixture
def written(tmp_path: Path, checkout: Path) -> Path:
    output = tmp_path / "out"
    call_command("styleguide", output=str(output), verbosity=0)
    return output


@pytest.mark.usefixtures("checkout")
class TestTheStyleGuide:
    def test_writes_the_page_next_to_the_built_assets(self, written: Path) -> None:
        assert (written / "index.html").is_file()
        assert (written / "static" / "dist" / "app.css").is_file()
        assert (written / "static" / "vendor" / "fonts" / "a.woff2").is_file()
        assert (written / "static" / "js" / "app.js").is_file()
        assert (written / "styleguide.js").read_text(encoding="utf-8").startswith("//")

    def test_the_page_is_clean_for_the_csp(self, written: Path) -> None:
        assert_csp_clean((written / "index.html").read_text(encoding="utf-8"))

    def test_draws_every_component(self, written: Path) -> None:
        html = (written / "index.html").read_text(encoding="utf-8")
        marks = {
            "button": "btn-primary",
            "card": "card-title",
            "empty_state": "border-dashed",
            "field": "field-error",
            "status_chip": "chip-ok",
            "table": "table-responsive",
            "page_header": 'data-testid="page-header"',
            "stat_tile": 'data-testid="stat-tile"',
            "progress": "<meter",
            "skeleton": 'aria-busy="true"',
            "menu": "popovertarget",
            "tabs": 'class="tabs"',
            "timeline": 'data-testid="timeline"',
            "definition_list": 'data-testid="facts"',
            "avatar": 'class="avatar"',
            "pagination": 'aria-label="Pagination"',
            "cursor_pagination": "Previous",
            "list_toolbar": "Rows per page",
            "list_summary": "results",
            "icon": "<svg",
            "filter_form": 'role="search"',
        }
        drawn = {path.stem for path in COMPONENTS.glob("*.html")}
        # sort_header sits in a table header and messages needs a request: own tests.
        assert drawn - {"sort_header", "messages", "delivery_badge"} <= set(marks), drawn - set(
            marks
        )
        for name, mark in marks.items():
            assert mark in html, name

    def test_every_vendored_icon_is_listed(self, written: Path) -> None:
        from apps.core.templatetags.ui_tags import icon_names

        html = (written / "index.html").read_text(encoding="utf-8")
        assert f"Icons ({len(icon_names())})" in html

    @pytest.mark.parametrize("theme", ["light", "dark"])
    def test_a_theme_can_be_chosen(self, tmp_path: Path, theme: str) -> None:
        call_command("styleguide", output=str(tmp_path / "out"), theme=theme, verbosity=0)
        html = (tmp_path / "out" / "index.html").read_text(encoding="utf-8")
        assert re.search(rf'<html lang="en"\s+data-theme="{theme}">', html)

    def test_system_writes_no_theme(self, written: Path) -> None:
        html = (written / "index.html").read_text(encoding="utf-8")
        assert re.search(r'<html lang="en"\s*>', html)


class TestRefusals:
    def test_refuses_production_settings(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("DJANGO_SETTINGS_MODULE", "config.settings.prod")
        with pytest.raises(CommandError, match="refuses to run with production settings"):
            call_command("styleguide", output=str(tmp_path))
        assert not (tmp_path / "index.html").exists()

    def test_asks_for_the_css_first(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        from apps.core.management.commands import styleguide

        monkeypatch.setattr(styleguide, "settings", Settings(tmp_path))
        with pytest.raises(CommandError, match=r"static/dist/app\.css is missing"):
            call_command("styleguide", output=str(tmp_path / "out"))

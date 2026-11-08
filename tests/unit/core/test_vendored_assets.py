"""Vendored front-end assets (ADR-0027): each file matches the hash written in its record, has
its licence beside it, and every icon a template names is in the vendored subset."""

import hashlib
import json
import re
from pathlib import Path

import pytest

from apps.core.templatetags.ui_tags import LEGACY_ICONS, icon_names

ROOT = Path(__file__).resolve().parents[3]
VENDOR = ROOT / "static" / "vendor"
README = (VENDOR / "README.md").read_text(encoding="utf-8")

# file → (sha256, licence file, size limit in bytes)
ASSETS = {
    "chartjs/chart.umd.min.js": (
        "84d0e233daba702b8f77d669d8c137cad36d441a10f200b6f2d3ab553bdfcf6b",
        "chartjs/LICENSE",
        250_000,
    ),
    "fonts/inter-latin-wght-normal.woff2": (
        "3100e775e8616cd2611beecfa23a4263d7037586789b43f035236a2e6fbd4c62",
        "fonts/Inter.LICENSE",
        120_000,
    ),
    "fonts/inter-latin-ext-wght-normal.woff2": (
        "34b9c504cab7a73e37b746343a449132e56cf7b5481af2cb81dc74dcff25c956",
        "fonts/Inter.LICENSE",
        120_000,
    ),
    "lucide/icons.json": (
        "f19ce98f11eefec6e75b5ea05cd18de0303a3c0c05dd72c98c765e53e8d5eafa",
        "lucide/LICENSE",
        60_000,
    ),
}
TEMPLATE_ICON = re.compile(r"""\{%\s*icon\s+["']([a-z0-9-]+)["']""")
ARGUMENT_ICON = re.compile(r"""\b(?:icon|primary_icon|chip_icon)=["']([a-z0-9-]+)["']""")
LEGACY_INCLUDE = re.compile(r"""components/icon\.html"\s+with\s+name=["']([a-z0-9-]+)["']""")
COMMENT = re.compile(r"\{#.*?#\}|\{% comment %\}.*?\{% endcomment %\}", re.S)


@pytest.mark.parametrize("name", sorted(ASSETS))
class TestEachAsset:
    def test_matches_the_recorded_hash(self, name: str) -> None:
        digest = hashlib.sha256((VENDOR / name).read_bytes()).hexdigest()
        assert digest == ASSETS[name][0], f"{name} changed: update static/vendor/README.md and here"

    def test_the_hash_is_in_the_readme(self, name: str) -> None:
        assert ASSETS[name][0] in README

    def test_has_its_licence_and_stays_small(self, name: str) -> None:
        _, licence, limit = ASSETS[name]
        assert (VENDOR / licence).read_text(encoding="utf-8").strip()
        assert (VENDOR / name).stat().st_size <= limit


def test_chart_library_is_the_umd_build_with_no_source_map_reference() -> None:
    text = (VENDOR / "chartjs" / "chart.umd.min.js").read_text(encoding="utf-8")
    assert text.startswith("/*!\n * Chart.js v4.")
    assert "sourceMappingURL" not in text
    assert "eval(" not in text
    assert "new Function" not in text


def test_the_readme_records_the_registry_integrity_of_every_tarball() -> None:
    for integrity in (
        "sha512-GIjfiT9dbmHRiYi6Nl2yFCq7kkwdkp1W/lp2J99rX0yo9tgJGn3lKQATztIjb5tVtevcBtIdICNWqlq5+E8/Pw==",
        "sha512-OupL48va4JNofb97w6NYeF9S7W/kHNKM0Er8Dem5nqi4jeOLrVJDoE8tZEpnMJmtkvNbB1EIPPwHcdkF6b1oUA==",
        "sha512-yLAiQB3MWUJEstsEDAwenSA3m7vr1tdLvgnC16Q/XMxRRma0DWwMs8M2bWlvgShrIs1m2mWMJ+0AiahqQO4GrA==",
    ):
        assert integrity in README


class TestIcons:
    def test_every_shape_is_one_the_tag_draws(self) -> None:
        data = json.loads((VENDOR / "lucide" / "icons.json").read_text(encoding="utf-8"))
        shapes = {tag for icon in data.values() for tag, _ in icon}
        assert shapes <= {"circle", "ellipse", "line", "path", "polygon", "polyline", "rect"}
        assert list(data) == sorted(data)

    def test_every_icon_a_template_names_exists(self) -> None:
        known = set(icon_names())
        used: dict[str, set[str]] = {}
        roots = [ROOT / "templates", *(ROOT / "apps").glob("*/templates")]
        for root in roots:
            for path in root.rglob("*.html"):
                text = COMMENT.sub("", path.read_text(encoding="utf-8"))
                legacy = [LEGACY_ICONS.get(name, name) for name in LEGACY_INCLUDE.findall(text)]
                for name in (*TEMPLATE_ICON.findall(text), *ARGUMENT_ICON.findall(text), *legacy):
                    used.setdefault(name, set()).add(path.relative_to(ROOT).as_posix())
        missing = {name: sorted(paths) for name, paths in used.items() if name not in known}
        assert not missing, f"add them with scripts/vendor_icons.py: {missing}"
        assert used, "the scan found no icon: the pattern is broken"


def test_every_url_in_the_stylesheet_is_a_vendored_file() -> None:
    """The built CSS lives in ``static/dist``: ``url(../vendor/…)`` must name a file that ships
    (collectstatic rewrites it to the hashed name and fails on a missing one)."""
    css = (ROOT / "static" / "src" / "app.css").read_text(encoding="utf-8")
    urls = re.findall(r"""url\(["']?([^"')]+)["']?\)""", css)
    assert len(urls) == 2  # the two Inter files
    for url in urls:
        assert url.startswith("../vendor/fonts/"), url
        assert (ROOT / "static" / "dist" / url).resolve().is_file(), url

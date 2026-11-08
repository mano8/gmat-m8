"""Rules every template keeps (ADR-0027): the CSP, mobile first, and no raw colours.

A template has no ``style`` attribute and no inline script, a layout is never shaped by a
``max-*`` variant (unprefixed classes are the phone layout; wider screens only add), and a
colour is always a token. These tests read the source, so a template nobody renders yet is
covered too.
"""

import re
from collections.abc import Iterator
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
TEMPLATE_ROOTS = [ROOT / "templates", *sorted((ROOT / "apps").glob("*/templates"))]

STYLE_ATTRIBUTE = re.compile(r"""\sstyle\s*=""", re.I)
STYLE_ELEMENT = re.compile(r"<style\b", re.I)
SCRIPT = re.compile(r"<script\b([^>]*)>", re.I)
HANDLER = re.compile(r"\son[a-z]+\s*=", re.I)
MAX_VARIANT = re.compile(
    r"(?<![\w-])(?:[a-z0-9@:\[\]-]+:)?max-(?:sm|md|lg|xl|2xl|\[[^\]]+\]|\([^)]+\)):"
)
# A palette utility (bg-red-500, text-slate-700, border-white …) or an arbitrary colour value.
RAW_COLOUR = re.compile(
    r"""(?<![\w-])(?:[a-z-]+:)*(?:bg|text|border|ring|fill|stroke|from|to|via|divide|outline|accent|decoration|shadow|caret)-"""
    r"""(?:slate|gray|zinc|neutral|stone|red|orange|amber|yellow|lime|green|emerald|teal|cyan|sky|blue|indigo|violet|purple|fuchsia|pink|rose|white|black)(?:-\d{2,3})?(?![\w-])"""
    r"""|\[(?:#[0-9a-fA-F]{3,8}|rgba?\(|hsla?\(|oklch\()"""
)


def templates() -> Iterator[Path]:
    for root in TEMPLATE_ROOTS:
        yield from sorted(root.rglob("*.html"))


def lines_matching(pattern: re.Pattern[str]) -> list[str]:
    found = []
    for path in templates():
        text = re.sub(
            r"\{#.*?#\}|\{% comment %\}.*?\{% endcomment %\}",
            "",
            path.read_text(encoding="utf-8"),
            flags=re.S,
        )
        for line in text.splitlines():
            if pattern.search(line):
                found.append(f"{path.relative_to(ROOT).as_posix()}: {line.strip()[:100]}")
    return found


def test_the_scan_sees_the_templates() -> None:
    assert len(list(templates())) > 100


def test_no_style_attribute_or_element() -> None:
    assert lines_matching(STYLE_ATTRIBUTE) == []
    assert lines_matching(STYLE_ELEMENT) == []


def test_no_inline_event_handler() -> None:
    assert lines_matching(HANDLER) == []


def test_scripts_are_files_or_data_blocks() -> None:
    offenders = []
    for path in templates():
        for attributes in SCRIPT.findall(path.read_text(encoding="utf-8")):
            if "src=" not in attributes and 'type="application/json"' not in attributes:
                offenders.append(path.relative_to(ROOT).as_posix())
    assert offenders == []


def test_no_max_width_variant_shapes_a_layout() -> None:
    """Mobile first: an exception needs a comment naming why (a print rule, say)."""
    assert lines_matching(MAX_VARIANT) == []


def test_no_raw_colour() -> None:
    assert lines_matching(RAW_COLOUR) == []


class TestTheRulesNotice:
    """The patterns do catch what they are written for."""

    @pytest.mark.parametrize(
        "line",
        ['<p style="width:3px">', '<div class="x" STYLE = "a">'],
    )
    def test_style_attribute(self, line: str) -> None:
        assert STYLE_ATTRIBUTE.search(line)

    @pytest.mark.parametrize(
        "line",
        [
            'class="max-sm:hidden"',
            'class="md:max-lg:flex"',
            'class="hover:max-md:p-2"',
            'class="max-[40rem]:grid"',
        ],
    )
    def test_max_variants(self, line: str) -> None:
        assert MAX_VARIANT.search(line)

    @pytest.mark.parametrize(
        "line", ['class="max-w-md"', 'class="max-h-20 md:max-w-5xl"', 'class="sm:grid"']
    )
    def test_max_size_utilities_are_fine(self, line: str) -> None:
        assert not MAX_VARIANT.search(line)

    @pytest.mark.parametrize(
        "line",
        [
            'class="bg-red-500"',
            'class="md:text-slate-700"',
            'class="border-white"',
            'class="bg-[#fff]"',
            'class="text-[oklch(0.5 0.1 20)]"',
        ],
    )
    def test_raw_colours(self, line: str) -> None:
        assert RAW_COLOUR.search(line)

    @pytest.mark.parametrize(
        "line",
        ['class="bg-surface text-soft border-line-strong"', 'class="text-danger bg-chart-1"'],
    )
    def test_tokens_are_fine(self, line: str) -> None:
        assert not RAW_COLOUR.search(line)

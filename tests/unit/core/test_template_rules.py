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
# Django's BlockNode puts its own `block` in the context inside every {% block %}, so a template
# variable or component parameter of that name is always set there (issue #72). Only the tags
# {% block %} / {% endblock %} and {{ block.super }} may use the word.
BLOCK_VARIABLE = re.compile(
    r"""\{\{[^}]*(?<![\w."'-])block\b(?!\.super)"""
    r"""|\{%(?!\s*(?:end)?block\b)(?:[^%"']|"[^"]*"|'[^']*')*(?<![\w."'-])block\b(?!\.super)"""
    r"""|\bblock\s*="""
)
RESERVED_PARAMETERS = ("block", "forloop", "request", "user", "perms", "messages", "csrf_token")
# An include or `with` that hands one of the names Django or a context processor sets to a
# component: it would shadow the real one in that component (a tag may span lines).
RESERVED_ARGUMENT = re.compile(
    r"\{%\s*(?:include|with)\b(?:[^%\"']|\"[^\"]*\"|'[^']*')*?(?<![\w.\"'-])"
    rf"(?:{'|'.join(RESERVED_PARAMETERS)})\s*=(?!=)"
)
# What a component header calls its parameters: `Parameters:`, `Parameter:` or `Arguments:`, up
# to the next blank line; an entry starts after the colon, a semicolon or a comma.
PARAMETERS_LINE = re.compile(r"(?:Parameters?|Arguments?):(.*?)(?:\n\s*\n|\Z)", re.S)
RESERVED_ENTRY = re.compile(rf"(?:^|[;,:])\s*`?(?:{'|'.join(RESERVED_PARAMETERS)})`?(?![\w.-])")


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


def tags_matching(pattern: re.Pattern[str]) -> list[str]:
    """Like ``lines_matching`` for a pattern that may span lines: the line where it starts."""
    found = []
    for path in templates():
        text = re.sub(
            r"\{#.*?#\}|\{% comment %\}.*?\{% endcomment %\}",
            lambda m: "\n" * m.group(0).count("\n"),
            path.read_text(encoding="utf-8"),
            flags=re.S,
        )
        for match in pattern.finditer(text):
            line = text.count("\n", 0, match.start()) + 1
            found.append(f"{path.relative_to(ROOT).as_posix()}:{line}: {match.group(0)[:80]!r}")
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


def test_no_template_reads_block_as_a_variable() -> None:
    """A component parameter named like a name Django sets in the context is always on (#72)."""
    assert tags_matching(BLOCK_VARIABLE) == []


def test_no_include_hands_a_reserved_name_to_a_component() -> None:
    assert tags_matching(RESERVED_ARGUMENT) == []


def test_no_component_lists_a_reserved_name_as_a_parameter() -> None:
    offenders = []
    for path in sorted((ROOT / "templates" / "components").glob("*.html")):
        header = re.search(r"\{% comment %\}(.*?)\{% endcomment %\}", path.read_text("utf-8"), re.S)
        if not header:
            continue
        for found in PARAMETERS_LINE.findall(header.group(1)):
            offenders += [
                f"{path.name}: {m.group(0).strip(' ;,:`')}" for m in RESERVED_ENTRY.finditer(found)
            ]
    assert offenders == []


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

    @pytest.mark.parametrize(
        "line",
        [
            "{% if block %}x{% endif %}",
            "{{ block }}",
            '{% include "components/button.html" with block=True %}',
            "{% with b=block %}",
        ],
    )
    def test_block_as_a_variable(self, line: str) -> None:
        assert BLOCK_VARIABLE.search(line)

    @pytest.mark.parametrize(
        "line",
        [
            "{% block content %}",
            "{% endblock %}",
            "{% endblock content %}",
            "{{ block.super }}",
            '{% trans "block" %}',
            '{% icon "x" classes="hidden md:block" %}',
            "{% if blocked %}",
        ],
    )
    def test_block_tags_and_super_are_fine(self, line: str) -> None:
        assert not BLOCK_VARIABLE.search(line)

    @pytest.mark.parametrize(
        "text",
        [
            '{% include "components/button.html" with label=x\n   block=True %}',
            '{% include "components/x.html" with user=person %}',
            "{% with request=item %}",
            '{% include "c.html" with a=1 messages=m %}',
        ],
    )
    def test_a_reserved_argument(self, text: str) -> None:
        assert RESERVED_ARGUMENT.search(text)

    @pytest.mark.parametrize(
        "text",
        [
            '{% include "c.html" with requester=x users=y %}',
            '{% include "c.html" with label="user=x" %}',
            "{% if user.is_authenticated %}",
            '{% include "c.html" with full_width=True %}',
        ],
    )
    def test_other_arguments_are_fine(self, text: str) -> None:
        assert not RESERVED_ARGUMENT.search(text)

    @pytest.mark.parametrize(
        "found",
        [
            " label; user the person shown; size",
            " label, request (the request)",
            " `block` (true for full width)",
            " name; perms",
        ],
    )
    def test_a_reserved_parameter_entry(self, found: str) -> None:
        assert RESERVED_ENTRY.search(found)

    @pytest.mark.parametrize(
        "found",
        [
            " label; requester (who asked); size",
            " label (the user's name); icon",
            " name (a person's name or email)",
            " full_width (never named `block`: Django sets that name)",
        ],
    )
    def test_other_parameter_entries_are_fine(self, found: str) -> None:
        assert not RESERVED_ENTRY.search(found)

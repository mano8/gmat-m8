"""scripts/check_contrast.py (ADR-0027): the contrast gate over the design tokens."""

from collections.abc import Callable
from pathlib import Path
from types import ModuleType

import pytest

ROOT = Path(__file__).resolve().parents[3]
CSS = (ROOT / "static" / "src" / "app.css").read_text(encoding="utf-8")


@pytest.fixture
def gate(load_script: Callable[[str], ModuleType]) -> ModuleType:
    return load_script("check_contrast")


def failures(gate: ModuleType, css: str) -> list[str]:
    themes, problems = gate.themes(css)
    for name, tokens in themes.items():
        problems += gate.check_theme(name, tokens)[1]
    return list(problems)


class TestTheStylesheet:
    def test_both_palettes_pass(self, gate: ModuleType) -> None:
        assert failures(gate, CSS) == []

    def test_main_prints_a_table_and_succeeds(
        self, gate: ModuleType, capsys: pytest.CaptureFixture[str]
    ) -> None:
        assert gate.main(["check_contrast.py", "--table"]) == 0
        out = capsys.readouterr().out
        assert "ink on surface" in out
        assert "all pass" in out

    def test_the_two_dark_blocks_are_the_same(self, gate: ModuleType) -> None:
        raw = gate.read_tokens(CSS)
        assert raw["dark-system"] == raw["dark-choice"]
        assert {"surface", "ink", "chart-1", "seq-5"} <= raw["dark-choice"].keys()

    def test_the_light_palette_defines_every_token_the_gate_reads(self, gate: ModuleType) -> None:
        tokens = gate.read_tokens(CSS)["light"]
        wanted = {pair.fg for pair in gate.PAIRS} | {pair.bg for pair in gate.PAIRS}
        assert wanted <= tokens.keys()


class TestItFails:
    def test_for_a_text_colour_too_close_to_its_background(self, gate: ModuleType) -> None:
        weak = CSS.replace("--ink-soft: var(--neutral-600);", "--ink-soft: var(--neutral-400);")
        assert any("ink-soft on surface" in line for line in failures(gate, weak))

    def test_for_a_dark_block_that_drifted(self, gate: ModuleType) -> None:
        marker = "  --brand-hover: oklch(0.8 0.1 250);\n  --brand-ink"
        assert marker in CSS
        drifted = CSS.replace(marker, "  --brand-hover: oklch(0.7 0.1 250);\n  --brand-ink")
        assert any(
            "the two dark blocks differ: brand-hover" in line for line in failures(gate, drifted)
        )

    def test_for_chart_colours_that_look_alike(self, gate: ModuleType) -> None:
        same = CSS.replace("--chart-2: oklch(0.66 0.17 52);", "--chart-2: oklch(0.5 0.17 259);")
        assert any("chart-1 and chart-2 look alike" in line for line in failures(gate, same))

    def test_for_a_ramp_out_of_order(self, gate: ModuleType) -> None:
        flat = CSS.replace("--seq-3: oklch(0.7 0.13 254);", "--seq-3: oklch(0.96 0.02 254);")
        assert any("sequential ramp" in line for line in failures(gate, flat))

    def test_for_a_token_it_cannot_read(self, gate: ModuleType) -> None:
        broken = CSS.replace("--focus: var(--brand-600);", "--focus: banana;")
        assert any("not a colour this gate reads" in line for line in failures(gate, broken))

    def test_for_a_missing_token(self, gate: ModuleType) -> None:
        missing = CSS.replace("  --info: oklch(0.48 0.09 215);\n", "")
        assert any("--info" in line for line in failures(gate, missing))

    def test_main_exits_nonzero(
        self,
        gate: ModuleType,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        broken = tmp_path / "app.css"
        broken.write_text(CSS.replace("--ink: var(--neutral-900);", "--ink: var(--neutral-300);"))
        monkeypatch.setattr(gate, "CSS", broken)
        assert gate.main(["check_contrast.py"]) == 1
        assert "FAILED" in capsys.readouterr().out


class TestColourMaths:
    def test_black_on_white_is_twenty_one(self, gate: ModuleType) -> None:
        assert gate.ratio(gate.to_linear("#000000"), gate.to_linear("#ffffff")) == pytest.approx(21)

    def test_oklch_white_and_black(self, gate: ModuleType) -> None:
        assert gate.luminance(gate.to_linear("oklch(1 0 0)")) == pytest.approx(1, abs=1e-3)
        assert gate.luminance(gate.to_linear("oklch(0 0 0)")) == pytest.approx(0, abs=1e-6)

    def test_oklch_agrees_with_the_css_colour_spec(self, gate: ModuleType) -> None:
        # CSS Color 4: red is oklch(0.62796 0.25768 29.234) and its relative luminance 0.2126.
        red = gate.to_linear("oklch(0.62796 0.25768 29.234)")
        assert red == pytest.approx((1, 0, 0), abs=2e-3)
        assert gate.luminance(red) == pytest.approx(0.2126, abs=1e-3)

    def test_a_vision_deficiency_makes_red_and_green_close(self, gate: ModuleType) -> None:
        red, green = gate.to_linear("#cc3311"), gate.to_linear("#7a8f1a")
        seen = gate.distance(gate.simulate(red, "deutan"), gate.simulate(green, "deutan"))
        assert seen < gate.distance(red, green)

    def test_the_parser_ignores_a_print_block(self, gate: ModuleType) -> None:
        css = ":root { --a: #000000; } @media print { :root { --a: #ffffff; } }"
        assert gate.read_tokens(css)["light"] == {"a": "#000000"}

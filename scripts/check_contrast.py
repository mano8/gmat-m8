"""Contrast gate for the design tokens (ADR-0027, M8b.1): ``python scripts/check_contrast.py``.

Reads the colour tokens from ``static/src/app.css`` for the light theme and the dark one and
fails when a pair the application really draws falls under WCAG 2.2 AA: 4.5:1 for text, 3:1 for
large text, the border of a control, the focus ring and the chart colours. It also fails when

- the two places that spell the dark palette (the ``prefers-color-scheme`` block for System and
  the ``data-theme="dark"`` block for an explicit choice) differ;
- two chart colours look alike to someone with protan, deutan or tritan colour vision;
- the sequential ramp is not ordered by lightness.

Tokens are written once in OKLCH (or hex) and resolved through ``var()``; this script converts
them to sRGB itself, so it needs nothing but the standard library. ``--table`` prints every pair.
"""

import itertools
import math
import re
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

CSS = Path(__file__).resolve().parent.parent / "static" / "src" / "app.css"
DARK_QUERY = "@media (prefers-color-scheme: dark)"
DARK_SYSTEM = ":root:not([data-theme='light'])"
DARK_CHOICE = ":root[data-theme='dark']"
CHART = tuple(f"chart-{n}" for n in range(1, 7))
SEQUENTIAL = tuple(f"seq-{n}" for n in range(1, 6))
SURFACES = ("surface", "surface-raised", "surface-sunken")
TEXT, LARGE, GRAPHIC = 4.5, 3.0, 3.0
MIN_CVD_DISTANCE = (
    0.05  # OKLab distance between two chart colours as a colour-blind person sees them
)
MIN_RAMP_STEP = 0.06  # OKLab lightness between neighbouring steps of the sequential ramp

# Colour-vision deficiency, linear sRGB (Machado, Oliveira and Fernandes 2009, severity 1.0).
CVD = {
    "protan": (
        (0.152286, 1.052583, -0.204868),
        (0.114503, 0.786281, 0.099216),
        (-0.003882, -0.048116, 1.051998),
    ),
    "deutan": (
        (0.367322, 0.860646, -0.227968),
        (0.280085, 0.672501, 0.047413),
        (-0.011820, 0.042940, 0.968881),
    ),
    "tritan": (
        (1.255528, -0.076749, -0.178779),
        (-0.078411, 0.930809, 0.147602),
        (0.004733, 0.691367, 0.303900),
    ),
}

Rgb = tuple[float, float, float]


@dataclass(frozen=True)
class Pair:
    """A foreground drawn on a background, and the ratio it has to reach."""

    fg: str
    bg: str
    minimum: float
    use: str


def _text_pairs() -> list[Pair]:
    pairs = [Pair(fg, bg, TEXT, "body text") for fg in ("ink", "ink-soft") for bg in SURFACES]
    for tone in ("brand", "ok", "warn", "danger", "info"):
        pairs += [Pair(tone, bg, TEXT, f"{tone} text or link") for bg in SURFACES]
        pairs.append(Pair(tone, f"{tone}-soft", TEXT, f"{tone} chip and notice"))
        pairs.append(Pair("ink", f"{tone}-soft", TEXT, f"body text on a {tone} notice"))
    pairs += [
        Pair("brand-ink", "brand", TEXT, "primary button"),
        Pair("danger-ink", "danger", TEXT, "danger button"),
        Pair("ink-soft", "brand-soft", TEXT, "secondary text on a tinted band"),
    ]
    return pairs


def _graphic_pairs() -> list[Pair]:
    pairs = [Pair("line-strong", bg, GRAPHIC, "border of a control") for bg in SURFACES]
    pairs += [Pair("focus", bg, GRAPHIC, "focus ring") for bg in (*SURFACES, "brand-soft")]
    for colour in CHART:
        pairs += [Pair(colour, bg, GRAPHIC, "chart mark") for bg in SURFACES[:2]]
    pairs += [
        Pair(tone, "surface-raised", GRAPHIC, "status icon")
        for tone in ("ok", "warn", "danger", "info")
    ]
    return pairs


PAIRS = [*_text_pairs(), *_graphic_pairs()]


# ----------------------------------------------------------------------- reading the CSS
def _strip(css: str) -> str:
    """The stylesheet without comments, quotes made single, white space squeezed."""
    css = re.sub(r"/\*.*?\*/", "", css, flags=re.S)
    return re.sub(r"\s+", " ", css.replace('"', "'"))


def _blocks(css: str) -> list[tuple[tuple[str, ...], str]]:
    """Every ``selector { declarations }`` with the at-rules and selectors around it."""
    found: list[tuple[tuple[str, ...], str]] = []
    path: list[str] = []
    start = 0
    body_start: list[int] = []
    for index, char in enumerate(css):
        if char == "{":
            path.append(css[start:index].strip())
            body_start.append(index + 1)
            start = index + 1
        elif char == "}":
            found.append((tuple(path), css[body_start.pop() : index]))
            path.pop()
            start = index + 1
        elif char == ";" and not path:
            start = index + 1
    return found


def _declarations(body: str) -> dict[str, str]:
    """The custom properties a block declares (nested blocks excluded by the caller)."""
    flat = re.sub(r"\{[^{}]*\}", "", body)
    return dict(re.findall(r"--([\w-]+)\s*:\s*([^;]+?)\s*(?:;|$)", flat))


def read_tokens(css_text: str) -> dict[str, dict[str, str]]:
    """The raw custom properties of the three places that define colours."""
    places = {
        "light": (":root",),
        "dark-system": (DARK_QUERY, DARK_SYSTEM),
        "dark-choice": (DARK_CHOICE,),
    }
    blocks = _blocks(_strip(css_text))
    return {
        name: {
            k: v for path, body in blocks if path == where for k, v in _declarations(body).items()
        }
        for name, where in places.items()
    }


# ------------------------------------------------------------------------------ colours
def _resolve(name: str, tokens: dict[str, str], seen: tuple[str, ...] = ()) -> str:
    if name in seen or name not in tokens:
        raise KeyError(f"unknown or circular token --{name}")
    value = tokens[name]
    reference = re.fullmatch(r"var\(--([\w-]+)\)", value)
    return _resolve(reference.group(1), tokens, (*seen, name)) if reference else value


def _srgb_encode(value: float) -> float:
    value = min(1.0, max(0.0, value))
    return 12.92 * value if value <= 0.0031308 else 1.055 * value ** (1 / 2.4) - 0.055


def _lab_to_linear(lightness: float, a: float, b: float) -> Rgb:
    l_ = (lightness + 0.3963377774 * a + 0.2158037573 * b) ** 3
    m_ = (lightness - 0.1055613458 * a - 0.0638541728 * b) ** 3
    s_ = (lightness - 0.0894841775 * a - 1.2914855480 * b) ** 3
    return (
        4.0767416621 * l_ - 3.3077115913 * m_ + 0.2309699292 * s_,
        -1.2684380046 * l_ + 2.6097574011 * m_ - 0.3413193965 * s_,
        -0.0041960863 * l_ - 0.7034186147 * m_ + 1.7076147010 * s_,
    )


def to_linear(value: str) -> Rgb:
    """Linear-light sRGB (clipped to the gamut) of ``oklch(L C H)`` or ``#rrggbb``."""
    oklch = re.fullmatch(r"oklch\(\s*([\d.]+)%?\s+([\d.]+)\s+([\d.]+)(?:deg)?\s*\)", value)
    if oklch:
        lightness, chroma, hue = (float(part) for part in oklch.groups())
        lab = _lab_to_linear(
            lightness, chroma * math.cos(math.radians(hue)), chroma * math.sin(math.radians(hue))
        )
        return (*(min(1.0, max(0.0, channel)) for channel in lab),)  # type: ignore[return-value]
    hexa = re.fullmatch(r"#([0-9a-f]{6})", value.lower())
    if hexa:
        raw = [int(hexa.group(1)[i : i + 2], 16) / 255 for i in (0, 2, 4)]
        return (*((c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4) for c in raw),)  # type: ignore[return-value]
    raise ValueError(f"not a colour this gate reads: {value!r}")


def luminance(rgb: Rgb) -> float:
    return 0.2126 * rgb[0] + 0.7152 * rgb[1] + 0.0722 * rgb[2]


def ratio(foreground: Rgb, background: Rgb) -> float:
    lighter, darker = sorted((luminance(foreground), luminance(background)), reverse=True)
    return (lighter + 0.05) / (darker + 0.05)


def to_oklab(rgb: Rgb) -> Rgb:
    r, g, b = rgb
    l_ = (0.4122214708 * r + 0.5363325363 * g + 0.0514459929 * b) ** (1 / 3)
    m_ = (0.2119034982 * r + 0.6806995451 * g + 0.1073969566 * b) ** (1 / 3)
    s_ = (0.0883024619 * r + 0.2817188376 * g + 0.6299787005 * b) ** (1 / 3)
    return (
        0.2104542553 * l_ + 0.7936177850 * m_ - 0.0040720468 * s_,
        1.9779984951 * l_ - 2.4285922050 * m_ + 0.4505937099 * s_,
        0.0259040371 * l_ + 0.7827717662 * m_ - 0.8086757660 * s_,
    )


def simulate(rgb: Rgb, kind: str) -> Rgb:
    matrix = CVD[kind]
    return (
        *(min(1.0, max(0.0, sum(m * c for m, c in zip(row, rgb, strict=True)))) for row in matrix),
    )  # type: ignore[return-value]


def distance(first: Rgb, second: Rgb) -> float:
    return math.dist(to_oklab(first), to_oklab(second))


# -------------------------------------------------------------------------------- checks
def check_theme(name: str, tokens: dict[str, str]) -> tuple[list[str], list[str]]:
    """(report lines, failures) for one resolved theme."""
    report: list[str] = []
    failures: list[str] = []

    def colour(token: str) -> Rgb:
        return to_linear(_resolve(token, tokens))

    for pair in PAIRS:
        try:
            got = ratio(colour(pair.fg), colour(pair.bg))
        except (KeyError, ValueError) as error:
            failures.append(f"{name}: {pair.fg} on {pair.bg}: {error}")
            continue
        ok = got >= pair.minimum
        report.append(
            f"{name:5} {'ok  ' if ok else 'FAIL'} {got:5.2f}:1 (>= {pair.minimum}) "
            f"{pair.fg} on {pair.bg}: {pair.use}"
        )
        if not ok:
            failures.append(
                f"{name}: {pair.fg} on {pair.bg} is {got:.2f}:1, "
                f"needs {pair.minimum}:1 ({pair.use})"
            )
    failures += _chart_failures(name, colour)
    failures += _ramp_failures(name, colour)
    return report, failures


def _chart_failures(name: str, colour: Callable[[str], Rgb]) -> list[str]:
    failures = []
    for first, second in itertools.combinations(CHART, 2):
        for kind in ("normal", *CVD):
            a, b = colour(first), colour(second)
            if kind != "normal":
                a, b = simulate(a, kind), simulate(b, kind)
            if distance(a, b) < MIN_CVD_DISTANCE:
                failures.append(f"{name}: {first} and {second} look alike ({kind} vision)")
    return failures


def _ramp_failures(name: str, colour: Callable[[str], Rgb]) -> list[str]:
    lightness = [to_oklab(colour(step))[0] for step in SEQUENTIAL]
    steps = [b - a for a, b in itertools.pairwise(lightness)]
    increasing, decreasing = (
        all(s >= MIN_RAMP_STEP for s in steps),
        all(s <= -MIN_RAMP_STEP for s in steps),
    )
    if increasing or decreasing:
        return []
    return [f"{name}: the sequential ramp is not ordered by lightness in steps of {MIN_RAMP_STEP}"]


def themes(css_text: str) -> tuple[dict[str, dict[str, str]], list[str]]:
    """The light and dark palettes as written, and what is wrong with how they are written."""
    raw = read_tokens(css_text)
    problems: list[str] = []
    if raw["dark-system"] != raw["dark-choice"]:
        differing = sorted(
            k
            for k in raw["dark-system"].keys() | raw["dark-choice"].keys()
            if raw["dark-system"].get(k) != raw["dark-choice"].get(k)
        )
        problems.append(f"the two dark blocks differ: {', '.join(differing) or 'one is missing'}")
    light = raw["light"]
    dark = {**light, **raw["dark-choice"]}
    return {"light": light, "dark": dark}, problems


def main(argv: list[str]) -> int:
    resolved, failures = themes(CSS.read_text(encoding="utf-8"))
    lines: list[str] = []
    for name, tokens in resolved.items():
        report, found = check_theme(name, tokens)
        lines += report
        failures += found
    if "--table" in argv:
        print("\n".join(lines))
    for failure in failures:
        print(f"FAIL {failure}")
    print(f"{len(PAIRS)} pairs in 2 themes: {'FAILED' if failures else 'all pass'}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))

# ADR-0027: The UI follows one design system: tokens, a component library, vendored assets

- **Status:** Proposed (the maintainer's request for M8b, issue #50; accepted when the phase PR merges)
- **Date:** 2026-11-08
- **Governs:** `static/src/app.css`, `static/js/`, `static/vendor/chartjs/`, `static/vendor/fonts/`, `static/vendor/lucide/`, `templates/components/`, `templates/layouts/`, `apps/core/templatetags/ui_tags.py`, `apps/core/management/commands/styleguide.py`, `apps/core/templates/styleguide/`, `docs/design/`, `scripts/check_contrast.py`, `scripts/vendor_icons.py`
- **Amends:** [ADR-0001](0001-modular-monolith.md) (what `static/vendor/` may hold)

## Context

The interface grew screen by screen from a small set of Tailwind classes. The maintainer asked for a modern, clear and consistent one: dashboards with charts, modal dialogs for the forms nested in pages, a left menu of category dropdowns that can be hidden, both themes, and the same look on every view. The reference is the approved set of mockups, committed as `docs/design/reference-mockups.html`. Behaviour, access rules, URLs and the ledger do not change: this decision is about how things look and how secondary forms open.

## Decision

**Principles**, applied by every step of the phase:

- *Clear first.* One primary action per screen. Status is a chip with an icon and a word, never colour alone. Quantities sit right-aligned in tabular figures beside their unit.
- *Mobile first, untouched.* Each component and page is designed at 360 px first; wider screens only add, through `min-width` media queries and container queries. Unprefixed classes are the phone layout: no `max-sm:`, `max-md:`, `max-lg:` or `max-xl:` variant shapes a layout (`tests/unit/core/test_template_rules.py`). Targets are 44 px (`min-h-11`), larger under `pointer-coarse:`; nothing is reachable only by hover; inputs keep a 16 px font; dialogs are bottom sheets on phones; tables become cards; nothing scrolls sideways.
- *Calm surfaces.* Raised cards on a tinted ground, soft elevation, consistent radii; light and dark themes of equal quality; motion only under `motion-safe` and never needed to understand anything.
- *Server-rendered, htmx-enhanced.* Nothing here adds a JavaScript framework, Node tooling, a CDN, a runtime fetch to another host, a `style` attribute or an inline script.

**Tokens** (`static/src/app.css`, Tailwind v4 CSS-first), defined once in OKLCH:

- A neutral and a brand scale (`--neutral-50…950`, `--brand-50…950`), and semantic tokens chosen from them: surface, raised, sunken, line (a divider), line-strong (the border of a control), ink, soft, focus, brand, ok, warn, danger, info (each with its `-soft`), the solid-button inks, and the backdrop.
- Radius (`--radius-sm…2xl`), elevation (`--shadow-1…3`), a spacing rhythm (`--gutter`, `--card-pad`), motion durations and two easings.
- Six categorical chart colours (`--chart-1…6`, colour-blind safe) and a five-step sequential ramp (`--seq-1…5`).
- `--font-sans` (Inter, with the system stack as fallback) and `--font-mono` (the system's, for document numbers).
- The default Tailwind palette is removed (`--color-*: initial`): a colour exists only as a token, and a template that writes a palette colour or an arbitrary colour value fails `test_template_rules`.
- The dark theme re-maps tokens only, under `@media (prefers-color-scheme: dark) { :root:not([data-theme="light"]) }` and again under `:root[data-theme="dark"]`, so System, Light and Dark all hold; `color-scheme` follows. The two blocks must hold the same values (CSS cannot share them): `scripts/check_contrast.py` fails when they differ.
- **Contrast gate.** `scripts/check_contrast.py` reads the tokens for both themes and fails under WCAG 2.2 AA for the pairs the application draws (4.5:1 text, 3:1 control borders, focus ring, status icons and chart marks against the surface), when two chart colours look alike to a person with protan, deutan or tritan vision, or when the sequential ramp is not ordered by lightness. It runs in the `lint` job.

**Components** are Tailwind v4 `@utility` classes (`btn`, `card`, `chip`, `field`, …) in `app.css` and templates under `templates/components/`, each with a header comment naming its arguments. A variant sets only custom properties (`--btn-bg`, `--chip-fg`), so the order of rules never decides. Container queries (`@container`) make cards and panels follow their column; state is in CSS (`has-[:checked]:`, `aria-*:`, `user-invalid:`, `open:`, `backdrop:`, `starting:`) rather than script. A hand-written `table-responsive` table follows the viewport (it carries no container wrapper); components that live in a column use container queries. A pattern the reference lacks is drawn in the style guide first (`manage styleguide`, development settings only, no route) and shown in the pull request.

**Vendored front-end assets** live under `static/vendor/`, fetched once by hand and never at run time. Each is recorded in `static/vendor/README.md` with its file, version, source, the registry's integrity of the tarball it came from and a sha256 of the file kept here; `tests/unit/core/test_vendored_assets.py` fails when a file or its record changes alone. A licence file sits beside it. A version is at least two weeks old when chosen. A change to a vendored file (the Chart.js source-map line) is written in its row. The set: htmx 2.x, Chart.js 4 (UMD build), Inter (variable, Latin and Latin Extended woff2, at most 120 KB each, `font-display: swap`) and a subset of Lucide icons (ISC) in `lucide/icons.json`, drawn by `{% icon %}` with escaped attributes, never marked safe. `font-src 'self'` does not change.

**Dialogs** are native `<dialog>` elements opened by invoker commands (`commandfor`/`command`) or `app.js`, never by inline handlers. A heavy form opened in a dialog keeps its own GET page that works without JavaScript. The htmx contract that lets a form open in a dialog is M8b.3's and is recorded there.

**Charts** (Q63): Chart.js 4, loaded only on the pages that draw a chart; its data reaches the page only through `json_script`; its colours are read from the CSS tokens; a data table sits beside every chart (and is what shows without JavaScript); no plugin is fetched at run time. It draws on a `<canvas>` and sizes it through the CSSOM, which `style-src 'self'` allows; the end-to-end CSP listener proves it. Small marks (progress, a share, a sparkline) are `<meter>`, `<progress>` or server-rendered SVG, no library.

**Browsers** (Q67): evergreen browsers of the last two years (Chrome and Edge, Firefox, Safari including iOS). Dialogs and popovers open natively; `app.js` covers a browser without invoker commands. Reading never needs a script (`ACCESSIBILITY.md`).

**What stays out:** a JavaScript framework, Node tooling in the repository or at runtime, a CDN, a `style` attribute, an inline script or handler, a font or script from another host, a per-installation theme (a later setting if asked).

## Alternatives rejected

- **A component library from npm (a Tailwind UI kit, a web-component set):** needs Node in the build and a CDN or a bundler; the project's ground rule is none of those (ADR-0001, ADR-0008).
- **Raw colour values per component:** two themes and an accessibility gate need one table of values; with the default palette removed, a stray colour is an error instead of a drift.
- **`@apply` chains in a components layer:** long, order-dependent and hard to give variants; `@utility` classes with custom properties compose without surprises.
- **A different charting library (uPlot, ECharts, a server-side SVG):** the maintainer chose Chart.js (Q63); its fit with the CSP was checked, and the table beside each chart keeps the data readable without it.

## Consequences

- A change to the files above answers this ADR in its pull request; a new colour, font, icon or library is a change to the token table or to `static/vendor/README.md` first.
- `CLAUDE.md` (Stack, Layout, Commands) names the chart library, the style guide and the contrast gate; `ACCESSIBILITY.md` states what the gate closes.
- ADR-0001's text now says what `static/vendor/` holds.
- The reference mockups and their screenshots (`docs/design/`) are documentation: never served by the app, so their CDN fonts and chart script do not touch its CSP.

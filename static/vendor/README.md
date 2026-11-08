# Vendored front-end code

Nothing here is fetched at run time: no CDN, no `npm install` in the build. A file is added by
hand, checked against the registry's published integrity value, and recorded below (ADR-0027).
The rule for every asset: its file, version, source, the registry's integrity of the tarball it
came from, a sha256 of the file kept here (`tests/unit/core/test_vendored_assets.py` fails when a
file or this record changes alone) and its licence file beside it. A version is at least two weeks
old when it is chosen. Any change to a file kept here is written down in its row.

## htmx

| | |
| --- | --- |
| File | `htmx.min.js` (and `htmx.LICENSE`, Zero-Clause BSD) |
| Version | 2.0.11 (the newest 2.x; htmx 4 is not a drop-in replacement) |
| Source | `https://registry.npmjs.org/htmx.org/-/htmx.org-2.0.11.tgz`, file `package/dist/htmx.min.js` |
| Tarball integrity | `sha512-Thx/WtpeOQqSrqBCw/A1cwGJGg4UrVa3+sW0GmrM3p4gJgO89ecH4qtbnyzDDWFvBTqjnIMCgELTNt636dtamA==` (matches the registry) |
| `htmx.min.js` sha256 | `d6fdc75f204e6bdefa99b69bf1e6d4ac69b8a364f77929f45c13476b4000f717` |

`tests/unit/core/test_ui_shell.py` checks the sha256 above, so a changed file fails CI until this
record is updated on purpose.

How it is configured (`templates/base.html`): a `<meta name="htmx-config">` with
`historyCacheSize: 0` and `refreshOnHistoryMiss: true` (no page snapshot is ever stored, so a
logged-out browser keeps nothing of the previous user), `includeIndicatorStyles: false` (htmx would
inject an inline `<style>`), `allowEval: false`, `allowScriptTags: false`, `selfRequestsOnly: true`.
The CSRF token travels in `hx-headers` on `<body>`; there is no inline JavaScript anywhere.

To update: download the new tarball, compare its integrity with the registry's, extract
`dist/htmx.min.js`, replace the file, and update this table and the sha256 in the test.

## Chart.js

| | |
| --- | --- |
| File | `chartjs/chart.umd.min.js` (and `chartjs/LICENSE`, MIT) |
| Version | 4.5.1 (the UMD build, which defines `window.Chart`) |
| Source | `https://registry.npmjs.org/chart.js/-/chart.js-4.5.1.tgz`, file `package/dist/chart.umd.min.js` |
| Tarball integrity | `sha512-GIjfiT9dbmHRiYi6Nl2yFCq7kkwdkp1W/lp2J99rX0yo9tgJGn3lKQATztIjb5tVtevcBtIdICNWqlq5+E8/Pw==` (matches the registry) |
| sha256 of the file in the tarball | `48444a82d4edcb5bec0f1965faacdde18d9c17db3063d042abada2f705c9f54a` |
| `chart.umd.min.js` sha256 (kept here) | `84d0e233daba702b8f77d669d8c137cad36d441a10f200b6f2d3ab553bdfcf6b` |
| Change | the last line, `//# sourceMappingURL=chart.umd.min.js.map`, is removed: the source map is not vendored and the hashing static files storage fails on a reference it cannot find |

Loaded only by the pages that draw a chart (M8b.5), with its data in a `json_script` block, colours
read from the CSS tokens and a data table beside every chart. It draws on a `<canvas>` and sizes
it through the CSSOM, which the CSP (`style-src 'self'`) allows; no plugin is fetched at run time.

## Inter (typeface)

| | |
| --- | --- |
| Files | `fonts/inter-latin-wght-normal.woff2` (48 KB), `fonts/inter-latin-ext-wght-normal.woff2` (85 KB) and `fonts/Inter.LICENSE` (SIL Open Font License 1.1) |
| Version | `@fontsource-variable/inter` 5.3.0 (Inter 4.x, variable weight 100-900) |
| Source | `https://registry.npmjs.org/@fontsource-variable/inter/-/inter-5.3.0.tgz`, files `package/files/inter-latin-wght-normal.woff2` and `inter-latin-ext-wght-normal.woff2` |
| Tarball integrity | `sha512-OupL48va4JNofb97w6NYeF9S7W/kHNKM0Er8Dem5nqi4jeOLrVJDoE8tZEpnMJmtkvNbB1EIPPwHcdkF6b1oUA==` (matches the registry) |
| `inter-latin-wght-normal.woff2` sha256 | `3100e775e8616cd2611beecfa23a4263d7037586789b43f035236a2e6fbd4c62` |
| `inter-latin-ext-wght-normal.woff2` sha256 | `34b9c504cab7a73e37b746343a449132e56cf7b5481af2cb81dc74dcff25c956` |

The Latin file covers Spanish, French and English, accents and the euro sign; the Latin Extended
file is fetched only for characters outside it (`unicode-range`). `@font-face` is in
`static/src/app.css` with `font-display: swap` and the system stack as the fallback; the CSP's
`font-src 'self'` does not change. The monospace face for document numbers is the system's.

## Lucide icons (subset)

| | |
| --- | --- |
| File | `lucide/icons.json` (and `lucide/LICENSE`, ISC) |
| Version | `lucide-static` 1.61.0 |
| Source | `https://registry.npmjs.org/lucide-static/-/lucide-static-1.61.0.tgz`, file `package/icon-nodes.json` |
| Tarball integrity | `sha512-yLAiQB3MWUJEstsEDAwenSA3m7vr1tdLvgnC16Q/XMxRRma0DWwMs8M2bWlvgShrIs1m2mWMJ+0AiahqQO4GrA==` (matches the registry) |
| `icons.json` sha256 | `f19ce98f11eefec6e75b5ea05cd18de0303a3c0c05dd72c98c765e53e8d5eafa` |

Only the icons the application draws, as the shapes of a 24 x 24 box. `{% icon "name" %}`
(`apps/core/templatetags/ui_tags.py`) reads the file once and builds each SVG with escaped
attributes. To add an icon: extract `icon-nodes.json` from the tarball (check its integrity
first), run `python scripts/vendor_icons.py <path>/icon-nodes.json <every name, the old ones too>`,
and update the hash above and in the test. `tests/unit/core/test_vendored_assets.py` checks that
every icon a template names is in the file.

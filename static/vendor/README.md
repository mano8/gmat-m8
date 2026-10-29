# Vendored front-end code

Nothing here is fetched at run time: no CDN, no `npm install` in the build. A file is added by
hand, checked against the registry's published integrity value, and recorded below.

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

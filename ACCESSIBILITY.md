# Accessibility

gmat-m8 is used on worksites as much as in offices: technicians request materials from a phone, often outdoors, and managers review on a desktop. The application has to work for people using a keyboard, a screen reader, zoomed text or a small screen, in any of its three languages (Spanish, English, French).

The project makes no formal conformance claim. [WCAG 2.2](https://www.w3.org/TR/WCAG22/) level AA is the reference used to judge accessibility reports and changes.

## What is in place

| Area | How | Where |
| --- | --- | --- |
| Language | `<html lang>` follows the active language; every UI string goes through gettext (`es` default, `en`, `fr`) | `templates/base.html`, `locale/` |
| Keyboard | A "Skip to content" link is the first focusable element; every focused element shows a visible outline (`:focus-visible`) | `templates/base.html`, `static/src/app.css` |
| Structure | One `<main id="main">`; the left menu and the phone's bottom bar are `<nav>` landmarks with a label, the categories are native `<details>` dropdowns, the current page carries `aria-current="page"`, and a menu the person hid has the `hidden` attribute, so it leaves the tab order and the landmarks. The menu button states what it does ("Hide the menu" / "Show the menu") and its `aria-expanded`; the user menu and the More sheet are popovers: Esc closes them and focus returns to their button | `templates/layouts/app.html`, `apps/core/navigation.py` |
| Forms | Every field has a `<label for>`, with its help text and errors grouped beside it | `templates/components/field.html`, `templates/allauth/elements/field.html` |
| Messages | Flash messages sit in a labelled region; errors use `role="alert"`, the others `role="status"`; the dismiss button has a label | `templates/components/messages.html` |
| Dialogs | Actions open in a native `<dialog>` opened as a modal: the page behind is inert, Tab stays inside, Esc closes it and the focus goes back to the button that opened it. A destructive confirmation states what it will do and puts the focus on the way out ("Cancel"). A form that comes back wrong stays in the open dialog with an error summary that takes the focus, its links leading to the fields. On a phone a dialog is a bottom sheet with 44 px buttons | `templates/components/dialog.html`, `static/js/app.js` |
| Menus and sheets | The account menu, the More sheet, the actions menu and the phone's filter sheet are popovers: Esc closes them and returns the focus to their button, and they close when the keyboard moves past their last item instead of staying open over the page | `templates/components/menu.html`, `static/js/app.js` |
| Charts | Every chart sits beside its data table ("Show the data", and what shows without JavaScript); the chart is a labelled image whose values the keyboard can walk (arrow keys, Esc), series differ by pattern and shape as well as colour, and nothing moves under `prefers-reduced-motion` | `apps/dashboards/`, `static/js/charts.js`, `templates/dashboards/` |
| Pagination | Labelled `<nav>`; unavailable links are marked `aria-disabled` | `templates/components/pagination.html` |
| Icons | Decorative SVG icons are `aria-hidden="true"` | `templates/components/icon.html` |
| Small screens | Mobile-first layouts, checked at 360 px with no horizontal scrolling; tables turn into cards on narrow screens; tap targets are at least 44 px high (`min-h-11`) | `templates/components/table.html`, `static/src/app.css` |
| Colour | Light and dark themes follow the system setting unless the person chooses one (System, Light or Dark, in the user menu and on the sign-in pages, remembered in a cookie), and come from shared colour tokens (OKLCH, defined once); a gate fails the build under WCAG 2.2 AA for body and secondary text, links, chips, notices, buttons, control borders, the focus ring, status icons and chart colours in both themes, and when two chart colours look alike with protan, deutan or tritan vision | `static/src/app.css`, `scripts/check_contrast.py` |
| Status | A status is a chip with an icon and a word, never colour alone; an error is an icon and a message, not only a red border | `templates/components/status_chip.html`, `templates/components/messages.html` |
| Type | Inter, self-hosted, with tabular figures for quantities and the system stack as fallback; inputs use a 16 px font so phones do not zoom | `static/src/app.css`, `static/vendor/fonts/` |
| Motion | Animations (spinner, skeleton, toasts, menus) only under `prefers-reduced-motion: no-preference`; nothing needs them to be understood | `static/src/app.css` |
| No script required to read | Pages are rendered on the server; htmx only enhances them | `templates/` |

Three automated layers check it (`tests/e2e/`, against a running stack):

- **axe-core** (`test_accessibility.py`) runs on the entrance pages, every role's home page and lists, and the document pages (the review page with a dialog open, the order, the receiving form), in the light and the dark theme, at 360 px and on a desktop, against the WCAG 2.0, 2.1 and 2.2 AA rules and axe's best practices. A serious or critical finding fails the run; everything else is written to `test-results/axe-report.txt` for the pull request. axe is injected with Playwright's `page.evaluate`, so the Content-Security-Policy stays as strict as in production, and the suite still fails on any violation it reports.
- **A keyboard walk** (`test_keyboard.py`): the tab order of the shell follows the document (no positive `tabindex`, the skip link first, the content after the menus), every stop shows an outline and is on screen, and each kind of dialog, the menus and the More sheet open, keep the focus and close from the keyboard alone.
- **The contrast gate** (`scripts/check_contrast.py`, in CI's `lint` job) for the colour tokens.

Screenshots of the main screens at 360 px and 1280 px, light and dark, are saved by `E2E_SCREENSHOTS=1` (`test-results/screens/`); a person compares them, there is no pixel baseline.

Unit tests assert parts of this (landmark labels, `aria-current`, hidden icons) in `tests/unit/core/test_ui_shell.py` and `tests/unit/core/test_components.py`; the contrast gate (`uv run python scripts/check_contrast.py`, in CI's `lint` job) has its own tests.

## Known gaps

- The Lighthouse accessibility score (target: 90 or more on the login and home pages) is a manual measure, since the repository has no Node tooling: run Lighthouse in Chrome on the pages above and put the score in the pull request. axe covers the same rules automatically, but Lighthouse has not been run.
- No audit with a screen reader (NVDA, VoiceOver, TalkBack) has been done. axe and the keyboard walk cannot say whether the announcements make sense in a language, in particular after an htmx swap or a dialog that comes back with errors.
- Chromium is the only browser the automated checks drive. Firefox was checked by hand on the style guide and the menus; Safari has not been checked.
- The date fields use the browser's own control; their picker button draws the browser's focus ring, not the application's.

These are tracked as work, not accepted as permanent.

## Reporting a problem

Open an issue with the **Accessibility problem** template (it adds the `accessibility` and `bug` labels). Say which page, which assistive technology or setting (screen reader, zoom level, keyboard only, high contrast, …), which browser and device, and what you expected. A barrier that keeps someone from finishing a task, such as submitting a request or recording a receipt, is treated as a bug, not as an enhancement.

## For contributors

A change to a template or to `static/src/app.css` should keep the list above true:

- reachable and usable with the keyboard alone, in a logical order, with a visible focus;
- a visible label for every control (a placeholder is not a label), and a text alternative for every meaningful icon or image;
- state not conveyed by colour alone (status chips also carry an icon and text);
- colours only as tokens, and a changed token passes `scripts/check_contrast.py` (it prints every pair with `--table`);
- usable at 360 px wide and with text zoomed to 200 %, in both themes;
- new strings translated (`makemessages -l es -l en -l fr`, then `compilemessages`);
- content swapped by htmx keeps focus in a sensible place and announces outcomes through the messages region.

Put a 360 px screenshot in the pull request, as the template asks.

# Accessibility

gmat-m8 is used on worksites as much as in offices: technicians request materials from a phone, often outdoors, and managers review on a desktop. The application has to work for people using a keyboard, a screen reader, zoomed text or a small screen, in any of its three languages (Spanish, English, French).

The project makes no formal conformance claim. [WCAG 2.2](https://www.w3.org/TR/WCAG22/) level AA is the reference used to judge accessibility reports and changes.

## What is in place

| Area | How | Where |
| --- | --- | --- |
| Language | `<html lang>` follows the active language; every UI string goes through gettext (`es` default, `en`, `fr`) | `templates/base.html`, `locale/` |
| Keyboard | A "Skip to content" link is the first focusable element; every focused element shows a visible outline (`:focus-visible`) | `templates/base.html`, `static/src/app.css` |
| Structure | One `<main id="main">`; the sidebar and the bottom bar are `<nav>` landmarks with a label, and the current page carries `aria-current="page"` | `templates/layouts/app.html`, `templates/partials/nav_items.html` |
| Forms | Every field has a `<label for>`, with its help text and errors grouped beside it | `templates/components/field.html`, `templates/allauth/elements/field.html` |
| Messages | Flash messages sit in a labelled region; errors use `role="alert"`, the others `role="status"`; the dismiss button has a label | `templates/components/messages.html` |
| Pagination | Labelled `<nav>`; unavailable links are marked `aria-disabled` | `templates/components/pagination.html` |
| Icons | Decorative SVG icons are `aria-hidden="true"` | `templates/components/icon.html` |
| Small screens | Mobile-first layouts, checked at 360 px with no horizontal scrolling; tables turn into cards on narrow screens; tap targets are at least 44 px high (`min-h-11`) | `templates/components/table.html`, `static/src/app.css` |
| Colour | Light and dark themes follow the system setting (`color-scheme: light dark`) and come from shared colour tokens | `static/src/app.css` |
| No script required to read | Pages are rendered on the server; htmx only enhances them | `templates/` |

Unit tests assert parts of this (landmark labels, `aria-current`, hidden icons) in `tests/unit/core/test_ui_shell.py`.

## Known gaps

- The Lighthouse accessibility score (target: 90 or more on the login and home pages) has not been measured yet.
- No audit with a screen reader (NVDA, VoiceOver, TalkBack) has been done.
- The contrast of the colour tokens in both themes has not been checked with a tool.

These are tracked as work, not accepted as permanent.

## Reporting a problem

Open an issue with the **Accessibility problem** template (it adds the `accessibility` and `bug` labels). Say which page, which assistive technology or setting (screen reader, zoom level, keyboard only, high contrast, …), which browser and device, and what you expected. A barrier that keeps someone from finishing a task, such as submitting a request or recording a receipt, is treated as a bug, not as an enhancement.

## For contributors

A change to a template or to `static/src/app.css` should keep the list above true:

- reachable and usable with the keyboard alone, in a logical order, with a visible focus;
- a visible label for every control (a placeholder is not a label), and a text alternative for every meaningful icon or image;
- state not conveyed by colour alone (status chips also carry text);
- usable at 360 px wide and with text zoomed to 200 %, in both themes;
- new strings translated (`makemessages -l es -l en -l fr`, then `compilemessages`);
- content swapped by htmx keeps focus in a sensible place and announces outcomes through the messages region.

Put a 360 px screenshot in the pull request, as the template asks.

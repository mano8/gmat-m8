# `templates/`

The shared templates. Loaded when you work in this directory; the UI rules that hold everywhere
(tokens only, mobile first, dialogs, `page_header`) are in the root `CLAUDE.md` (Conventions).

## Layout

- `layouts/`, `components/` (M8b.1: the library, each with a header comment naming its arguments: `button`, `card`, `status_chip`, `table`, `field`, the list components, `empty_state`, `messages`, `page_header`, `stat_tile`, `progress`, `skeleton`, `menu`, `tabs`, `timeline`, `definition_list`, `avatar`; icons are `{% icon "name" %}` and chips `{% chip %}` from `apps/core/templatetags/ui_tags.py`, a name missing from `static/vendor/lucide/icons.json` is added with `scripts/vendor_icons.py`), `partials/`, `allauth/` and `django/forms/` overrides, error pages
- The rules `tests/unit/core/test_template_rules.py` keeps: no `style` attribute, no inline script or handler, no `max-sm:`/`max-md:`/`max-lg:`/`max-xl:` variant, no raw colour, only tokens
- `manage styleguide` renders the library from `apps/core/templates/styleguide/`, `docs/design/` holds the reference mockups every view follows (ADR-0027)

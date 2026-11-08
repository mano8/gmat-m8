"""``manage styleguide [--output DIR] [--theme system|light|dark]``: the component library, drawn.

Renders every component of ``templates/components/`` in each variant and state, with the colour
tokens, the type and the icons, to ``build/styleguide/index.html`` (git-ignored) against the
built CSS (``static/dist/app.css``). Open the file in a browser, or in a phone-sized window, to
review a component beside ``docs/design/reference-mockups.html``. There is no route, so the page
is never served and has no access-matrix row; like ``seed_demo`` it refuses production settings.
"""

import os
import shutil
from datetime import timedelta
from pathlib import Path
from typing import Any

from django import forms
from django.conf import settings
from django.core.management.base import BaseCommand, CommandError, CommandParser
from django.template.loader import render_to_string
from django.test import RequestFactory
from django.utils import timezone, translation

from apps.core.dialogs import ActionDialog
from apps.core.listing import CURSOR_START, CursorPage, ListScope, paginate
from apps.core.templatetags.ui_tags import icon_names

ALLOWED_SETTINGS = frozenset({"config.settings.dev", "config.settings.test"})
DEFAULT_OUTPUT = settings.BASE_DIR / "build" / "styleguide"
STATIC_DIRS = ("dist", "vendor", "js")
THEMES = ("system", "light", "dark")
SCRIPT = """// Switches the theme of the style guide (the app does it with a cookie, M8b.2).
document.addEventListener("click", function (event) {
  var button = event.target.closest("[data-theme-choice]");
  if (!button) { return; }
  var theme = button.getAttribute("data-theme-choice");
  var root = document.documentElement;
  if (theme === "system") {
    root.removeAttribute("data-theme");
  } else {
    root.setAttribute("data-theme", theme);
  }
  document.querySelectorAll("[data-theme-choice]").forEach(function (other) {
    other.setAttribute("aria-pressed", String(other === button));
  });
});
"""


class SampleFilter(forms.Form):
    """A filter form for the list toolbar."""

    q = forms.CharField(label="Search", required=False)
    state = forms.ChoiceField(
        label="State", required=False, choices=[("", "Any"), ("open", "Open"), ("closed", "Closed")]
    )


class SampleForm(forms.Form):
    """Fields in each state: plain, helped, required, invalid."""

    reference = forms.CharField(label="Reference", help_text="Printed on the delivery note.")
    quantity = forms.DecimalField(label="Quantity", initial="12.500", help_text="In metres.")
    note = forms.CharField(label="Note", required=False, widget=forms.Textarea(attrs={"rows": 2}))
    kind = forms.ChoiceField(label="Kind", choices=[("a", "Delivery"), ("b", "Return")])
    sure = forms.BooleanField(label="I checked the goods", required=False)


class ReasonSample(forms.Form):
    """The reason of a destructive confirmation."""

    reason = forms.CharField(
        label="Reason", widget=forms.Textarea(attrs={"rows": 2}), help_text="Kept in the history."
    )


def _pages() -> dict[str, Any]:
    """Lists in the shapes the list components take (a counted page, a cursor page)."""
    request = RequestFactory().get("/styleguide/", {"page": "3"})
    counted = paginate(request, list(range(230)), default_per_page=10)
    cursor = CursorPage(
        rows=list(range(10)),
        per_page=10,
        start="b",
        next="c",
        back=("a",),
        scope=ListScope(),
    )
    first = CursorPage(rows=[], per_page=10, start=CURSOR_START, next="", back=())
    return {"request": request, "counted": counted, "cursor": cursor, "last_cursor": first}


def _context() -> dict[str, Any]:
    now = timezone.now()
    invalid = SampleForm(data={"reference": "", "quantity": "x", "kind": "a"})
    pages = _pages()
    return {
        **pages,
        "icons": icon_names(),
        "filter_form": SampleFilter({"state": "open"}),
        "form": SampleForm(),
        "invalid_form": invalid,
        # The dialogs' forms (M8b.3), with their own ids: the fields above use the same names.
        "reason_form": ReasonSample(auto_id="sg_reason_%s"),
        "dialog_form": SampleForm(
            data={"reference": "", "quantity": "x", "kind": "a"}, auto_id="sg_dialog_%s"
        ),
        # An action dialog (M8b.4): what the pages build from apps.core.dialogs.ActionDialog.
        "action_dialog": ActionDialog(
            id="sg-action-dialog",
            title="Void the receipt",
            description="REC-MAD-000124",
            icon="ban",
            tone="danger",
            action="#",
            submit="Void the receipt",
            form=ReasonSample(auto_id="sg_action_%s"),
            consequences=(
                "The goods leave the receipts of the order.",
                "The requests they served wait for them again.",
            ),
            warning="A handover made on site from these goods is voided with it.",
            cancel="Keep the receipt",
            testid="sg-action-form",
        ),
        "table_columns": ["Material", "Ordered", "Received"],
        "table_rows": [["Copper cable 3x2.5", "120", "80"], ["Plug, type F", "40", "40"]],
        "numeric_columns": [1, 2],
        "breadcrumbs": [("Receiving", "#"), ("REC-MAD-000124", "")],
        "menu_items": [
            {"label": "Print", "href": "#", "icon": "printer"},
            {"label": "Download", "href": "#", "icon": "download"},
            {"label": "Void receipt", "href": "#", "icon": "ban", "tone": "danger"},
        ],
        "tab_items": [
            {"label": "Lines", "href": "#", "current": True, "count": 4},
            {"label": "History", "href": "#", "count": 12},
            {"label": "Files", "href": "#"},
        ],
        "facts": [
            ("Supplier", "Cables Ibéricos S.A."),
            ("Project", "Nave 3 Getafe"),
            ("Needed by", "12 Nov 2026"),
            ("Priority", "Urgent"),
            ("Buyer", "Alfa Madrid"),
        ],
        "entries": [
            {
                "title": "Receipt posted",
                "detail": "80 of 120 received",
                "tone": "ok",
                "icon": "package-check",
                "when": now - timedelta(hours=2),
            },
            {
                "title": "Order amended",
                "detail": "Delivery moved to Friday",
                "tone": "brand",
                "icon": "pencil",
                "when": now - timedelta(days=1),
            },
            {"title": "Order issued", "tone": "", "icon": "send", "when": now - timedelta(days=3)},
        ],
        "spark_up": [3, 4, 4, 6, 5, 8, 9],
        "spark_down": [9, 8, 8, 6, 7, 4, 3],
    }


class Command(BaseCommand):
    help = "Render every UI component to build/styleguide/index.html (development only)."

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument("--output", default=str(DEFAULT_OUTPUT), help="Directory to write.")
        parser.add_argument("--theme", choices=THEMES, default="system")

    def handle(self, *args: Any, **options: Any) -> None:
        module = os.environ.get("DJANGO_SETTINGS_MODULE", "")
        if module not in ALLOWED_SETTINGS:
            raise CommandError(
                "styleguide refuses to run with production settings "
                f"(DJANGO_SETTINGS_MODULE={module or 'unset'}); it is for development only."
            )
        built = settings.BASE_DIR / "static" / "dist" / "app.css"
        if not built.is_file():
            raise CommandError(
                "static/dist/app.css is missing: build it first "
                "(tailwindcss --input static/src/app.css --output static/dist/app.css)."
            )
        output = Path(options["output"])
        self._copy_static(output)
        context = {**_context(), "theme": options["theme"]}
        with translation.override("en"):
            html = render_to_string("styleguide/index.html", context, request=context["request"])
        (output / "index.html").write_text(html, encoding="utf-8")
        (output / "styleguide.js").write_text(SCRIPT, encoding="utf-8")
        self.stdout.write(f"Wrote {output / 'index.html'}")

    @staticmethod
    def _copy_static(output: Path) -> None:
        for name in STATIC_DIRS:
            target = output / "static" / name
            shutil.rmtree(target, ignore_errors=True)
            shutil.copytree(settings.BASE_DIR / "static" / name, target)

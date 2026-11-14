"""The confirmation and reason dialogs of the screens (M8b.4).

One ``ActionDialog`` says everything a small action form needs: its title, the consequences it
states, the form, where it posts and what its buttons say. The page draws it with
``components/action_dialog.html``; the view that handles the POST builds the same object with
the bound form and hands it to ``apps.core.responses`` (``dialog=spec.dialog()``), so an
invalid or refused form comes back (422) as the same markup, inside the open dialog.
Authorization is the views' and services' as before: nothing here decides who may do what.
"""

from dataclasses import dataclass, replace
from typing import Any

from django.forms import Form
from django.http import HttpRequest, HttpResponse
from django.shortcuts import render
from django.utils.cache import patch_vary_headers
from django.utils.translation import gettext

from apps.core.http import is_htmx
from apps.core.responses import Dialog, varies

FRAGMENT = "components/dialog_form.html"
PAGE = "layouts/dialog_page.html"


@dataclass(frozen=True)
class ActionDialog:
    """An action form shown in a dialog.

    ``id`` is unique on the page (its title is ``<id>-title``). ``consequences`` are what the
    action will do, one sentence each (a destructive dialog states them); ``note`` is a single
    paragraph of help; ``warning`` a note that deserves more attention. ``tone`` is ``"danger"``
    for a destructive action: the danger badge, a danger submit button and Cancel focused first.
    ``body`` names a template drawn under the form's fields with ``data``, for what the plain
    texts cannot say (the preview of a scope change); a function as ``data`` is called only
    when the dialog is drawn, so a POST that succeeds never works it out.
    """

    id: str
    title: str
    action: str
    submit: str
    form: Form
    description: str = ""
    note: str = ""
    consequences: tuple[str, ...] = ()
    warning: str = ""  # something to weigh before going on, drawn as a note (warn tone)
    warning_testid: str = ""
    tone: str = ""
    cancel: str = ""
    testid: str = ""
    size: str = ""
    submit_testid: str = ""
    modal: bool = True  # False on the form's own page (opened as a link): Cancel goes ``back``
    back: str = ""
    label: str = ""  # what the button or menu item that opens it says (default: the title)
    icon: str = ""  # that button's icon, a Lucide name
    body: str = ""  # a template drawn under the fields, with ``data`` (M10.2: a preview)
    data: Any = None  # what ``body`` shows; a function is called only when it is drawn

    @property
    def opener_label(self) -> str:
        return self.label or self.title

    @property
    def open_testid(self) -> str:
        """``data-testid`` of the opener: the form's, ``-form`` replaced by ``-open``."""
        return f"{self.testid.removesuffix('-form')}-open" if self.testid else ""

    @property
    def dialog_testid(self) -> str:
        """``data-testid`` of the ``<dialog>``: the form's, ``-form`` becoming ``-dialog``."""
        return f"{self.testid.removesuffix('-form')}-dialog" if self.testid else ""

    def menu_item(self) -> dict[str, str]:
        """This dialog as an item of ``components/menu.html``."""
        return {
            "label": self.opener_label,
            "dialog": self.id,
            "icon": self.icon,
            "tone": self.tone,
            "testid": self.open_testid,
        }

    def with_form(self, form: Form) -> ActionDialog:
        """The same dialog around another form (the bound one, to show its errors)."""
        return replace(self, form=form)

    def dialog(self) -> Dialog:
        """What ``perform``/``invalid`` need to answer an htmx POST with this dialog again."""
        return Dialog(self.form, FRAGMENT, lambda bound: {"d": self.with_form(bound)})


def lazy(request: HttpRequest, spec: ActionDialog) -> HttpResponse:
    """The form of a lazy dialog (GET): its fragment for the page's dialog, which asks with htmx
    and names the dialog's content as the target (``HX-Target``, compared, never printed), or
    the whole page, so the URL still works as a link and without JavaScript (Q67).

    ``spec.id`` is the id of the page's shared dialog for this kind of action; ``spec.back``
    is where the page's own Cancel leads.
    """
    in_dialog = is_htmx(request) and request.headers.get("HX-Target") == f"{spec.id}-content"
    if in_dialog:
        response = render(request, FRAGMENT, {"d": spec})
    else:
        response = render(request, PAGE, {"d": replace(spec, modal=False)})
    patch_vary_headers(response, ["HX-Target"])
    return varies(response)


# --- what a membership and a project assignment share (both have a validity window) ------------

VALIDITY_DIALOG = "validity-dialog"
REVOKE_DIALOG = "revoke-dialog"


def validity_dialog(action: str, form: Form, *, back: str = "") -> ActionDialog:
    """Change the window a role or a project assignment is valid in: a lazy dialog."""
    return ActionDialog(
        id=VALIDITY_DIALOG,
        title=gettext("Change validity"),
        action=action,
        submit=gettext("Save"),
        form=form,
        back=back,
        testid="validity-form",
        submit_testid="validity-submit",
    )

"""The confirmation and reason dialogs of the screens (M8b.4).

One ``ActionDialog`` says everything a small action form needs: its title, the consequences it
states, the form, where it posts and what its buttons say. The page draws it with
``components/action_dialog.html``; the view that handles the POST builds the same object with
the bound form and hands it to ``apps.core.responses`` (``dialog=spec.dialog()``), so an
invalid or refused form comes back (422) as the same markup, inside the open dialog.
Authorization is the views' and services' as before: nothing here decides who may do what.
"""

from dataclasses import dataclass, replace

from django.forms import Form

from apps.core.responses import Dialog

FRAGMENT = "components/dialog_form.html"


@dataclass(frozen=True)
class ActionDialog:
    """An action form shown in a dialog.

    ``id`` is unique on the page (its title is ``<id>-title``). ``consequences`` are what the
    action will do, one sentence each (a destructive dialog states them); ``note`` is a single
    paragraph of help. ``tone`` is ``"danger"`` for a destructive action: the danger badge, a
    danger submit button and Cancel focused first.
    """

    id: str
    title: str
    action: str
    submit: str
    form: Form
    description: str = ""
    note: str = ""
    consequences: tuple[str, ...] = ()
    tone: str = ""
    cancel: str = ""
    testid: str = ""
    size: str = ""
    submit_testid: str = ""
    label: str = ""  # what the button or menu item that opens it says (default: the title)
    icon: str = ""  # that button's icon, a Lucide name

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

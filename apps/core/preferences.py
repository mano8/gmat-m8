"""The display preferences cookie (M8b.2, Q69): theme, left menu hidden, folded categories.

One cookie per browser, written only by ``/ui/preferences/`` in answer to a CSRF-protected POST
and never by a script. It holds no identifier, account, token or time: a fixed grammar of
allowlisted tokens joined by ``.`` (``dark.hidden.f-work.f-insights``), at most 128 bytes,
matched in full. A value that does not match, is too long or repeats a token is ignored as a
whole and the page renders the defaults; the next POST rewrites it. It is not signed: whatever
a person could forge here they could also choose with the controls, and it never reaches
authorization, querysets, cache keys or the session.
"""

import re
from dataclasses import dataclass
from typing import Any

from django import forms

#: ``""`` is System: follow the device's ``prefers-color-scheme``.
THEMES = ("light", "dark")
HIDDEN = "hidden"
#: The navigation's categories (``apps.core.navigation``), the only keys a cookie may fold.
CATEGORY_KEYS = ("work", "purchasing", "site", "organization", "insights", "administration")
FOLD_PREFIX = "f-"
MAX_COOKIE_BYTES = 128

_SHAPE = re.compile(rf"[a-z.-]{{1,{MAX_COOKIE_BYTES}}}")
_ALLOWED = frozenset({*THEMES, HIDDEN, *(FOLD_PREFIX + key for key in CATEGORY_KEYS)})


@dataclass(frozen=True)
class Preferences:
    """What the cookie says; the defaults are System, menu shown, every category open."""

    theme: str = ""
    menu_hidden: bool = False
    folded: frozenset[str] = frozenset()

    def serialize(self) -> str:
        """The canonical cookie value (empty when everything is the default)."""
        tokens = [self.theme] if self.theme else []
        if self.menu_hidden:
            tokens.append(HIDDEN)
        tokens.extend(FOLD_PREFIX + key for key in CATEGORY_KEYS if key in self.folded)
        return ".".join(tokens)


def _tokens(raw: object) -> list[str] | None:
    """The tokens of a value that is exactly the grammar, else ``None``."""
    if not isinstance(raw, str) or not _SHAPE.fullmatch(raw):
        return None
    tokens = raw.split(".")
    one_theme = sum(token in THEMES for token in tokens) <= 1
    if len(set(tokens)) != len(tokens) or not _ALLOWED.issuperset(tokens) or not one_theme:
        return None
    return tokens


def parse(raw: object) -> Preferences:
    """The preferences a cookie value stands for; anything that is not exactly the grammar
    gives the defaults."""
    tokens = _tokens(raw)
    if tokens is None:
        return Preferences()
    return Preferences(
        theme=next((token for token in tokens if token in THEMES), ""),
        menu_hidden=HIDDEN in tokens,
        folded=frozenset(
            token.removeprefix(FOLD_PREFIX) for token in tokens if token.startswith(FOLD_PREFIX)
        ),
    )


class PreferencesForm(forms.Form):
    """The POST of ``/ui/preferences/``: any of the three controls, each against the allowlist.

    ``theme`` is ``system``, ``light`` or ``dark``; ``menu`` is ``shown`` or ``hidden``; a
    folded category comes as ``fold`` (its key) with ``state`` (``open`` or ``closed``).
    """

    theme = forms.ChoiceField(required=False, choices=[(t, t) for t in ("system", *THEMES)])
    menu = forms.ChoiceField(required=False, choices=[("shown", "shown"), (HIDDEN, HIDDEN)])
    fold = forms.ChoiceField(required=False, choices=[(key, key) for key in CATEGORY_KEYS])
    state = forms.ChoiceField(required=False, choices=[("open", "open"), ("closed", "closed")])

    def clean(self) -> dict[str, Any]:
        data = super().clean() or {}
        if bool(data.get("fold")) != bool(data.get("state")):
            raise forms.ValidationError("fold and state go together")
        return data

    def apply(self, current: Preferences) -> Preferences:
        """``current`` with the controls this POST carried (the others are left as they are)."""
        data = self.cleaned_data
        theme = current.theme
        if data["theme"]:
            theme = "" if data["theme"] == "system" else data["theme"]
        menu_hidden = current.menu_hidden if not data["menu"] else data["menu"] == HIDDEN
        folded = set(current.folded)
        if data["fold"]:
            (folded.add if data["state"] == "closed" else folded.discard)(data["fold"])
        return Preferences(theme=theme, menu_hidden=menu_hidden, folded=frozenset(folded))

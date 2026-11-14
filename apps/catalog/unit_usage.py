"""What still uses a unit of measure, and so keeps it from being deactivated (Q72, ADR-0030).

A unit is deactivated only once nothing alive uses it: no active material and no line of a
document still in progress. The catalog counts its own materials; each document app registers
a provider for its lines from its ``AppConfig.ready``, as the organization's deactivation
preconditions do, so the catalog never imports the apps built on it.
"""

from collections.abc import Callable
from dataclasses import dataclass
from uuid import UUID

from django.utils.translation import ngettext

from apps.catalog.models import Material

EXAMPLES = 5  # the materials named in the dialog, the first by reference


@dataclass(frozen=True)
class UnitUse:
    """One kind of thing that uses the unit: how many, said in a sentence (``text``), and the
    first few by name when the administrator has to act on them (``examples``)."""

    kind: str
    count: int
    text: str
    examples: tuple[str, ...] = ()

    @property
    def more(self) -> bool:
        """Whether some of them are not named in ``examples``."""
        return self.count > len(self.examples)


type UnitUsersProvider = Callable[[UUID], UnitUse | None]

_providers: list[UnitUsersProvider] = []


def register_unit_users(provider: UnitUsersProvider) -> Callable[[], None]:
    """Register ``provider`` (from an ``AppConfig.ready``); return its unregister function."""
    if provider not in _providers:
        _providers.append(provider)

    def unregister() -> None:
        if provider in _providers:
            _providers.remove(provider)

    return unregister


def active_materials(unit_id: UUID) -> UnitUse | None:
    """The active materials measured in the unit, the first ``EXAMPLES`` by reference.

    An inactive material keeps its unit and blocks nothing: it is no longer requested.
    """
    rows = Material.objects.filter(unit_id=unit_id, is_active=True)
    count = rows.count()
    if not count:
        return None
    named = rows.order_by("reference").values_list("reference", flat=True)[:EXAMPLES]
    text = ngettext("%(count)d active material", "%(count)d active materials", count)
    return UnitUse("materials", count, text % {"count": count}, tuple(named))


def uses(unit_id: UUID) -> list[UnitUse]:
    """Everything alive that uses the unit, the materials first; empty when nothing does."""
    found = (provider(unit_id) for provider in (active_materials, *_providers))
    return [use for use in found if use is not None]

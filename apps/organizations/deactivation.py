"""Deactivation preconditions for companies and branches (plan §3.3, S9, F62).

A company or branch can be deactivated only when it has no issued purchase orders and no
approved requests with outstanding quantity. Those documents arrive with M4 and M5: each
document app registers a provider that lists its open documents for a set of branches,
and the deactivation service refuses with the full list.
"""

from collections.abc import Callable, Collection, Iterable
from dataclasses import dataclass
from uuid import UUID

from django.core.exceptions import ValidationError
from django.utils.translation import gettext_lazy as _


@dataclass(frozen=True, order=True)
class OpenDocument:
    """A document that blocks a deactivation, as shown to the administrator."""

    kind: str
    number: str
    id: UUID


type OpenDocumentsProvider = Callable[[Collection[UUID]], Iterable[OpenDocument]]

_providers: list[OpenDocumentsProvider] = []


def register_open_documents(provider: OpenDocumentsProvider) -> Callable[[], None]:
    """Register ``provider`` (from an ``AppConfig.ready``); return its unregister function."""
    if provider not in _providers:
        _providers.append(provider)

    def unregister() -> None:
        if provider in _providers:
            _providers.remove(provider)

    return unregister


def open_documents(branch_ids: Collection[UUID]) -> list[OpenDocument]:
    """Every open document of these branches, sorted, from every registered provider."""
    if not branch_ids:
        return []
    return sorted({doc for provider in _providers for doc in provider(branch_ids)})


class DeactivationRefused(ValidationError):
    """Open documents block the deactivation; ``documents`` lists them."""

    def __init__(self, documents: list[OpenDocument]) -> None:
        self.documents = documents
        listed = ", ".join(f"{doc.kind} {doc.number}" for doc in documents)
        super().__init__(
            _("Cannot deactivate while these documents are open: %(documents)s"),
            code="open_documents",
            params={"documents": listed},
        )

"""Field updates for services: an allow-list and an audit diff in one step."""

from collections.abc import Collection, Mapping
from typing import Any

from django.db.models import Model


def apply_changes(
    obj: Model, changes: Mapping[str, Any], allowed: Collection[str]
) -> dict[str, dict[str, Any]]:
    """Set each changed attribute on ``obj``; return ``{field: {"from": …, "to": …}}``.

    Raises ``ValueError`` for a field outside ``allowed``: a service decides what may
    change, never its caller. Foreign keys are passed by attribute name (``branch_id``).
    """
    unknown = sorted(set(changes) - set(allowed))
    if unknown:
        raise ValueError(f"{type(obj).__name__} fields that cannot be changed here: {unknown}")
    diff: dict[str, dict[str, Any]] = {}
    for name, value in changes.items():
        old = getattr(obj, name)
        if old != value:
            diff[name] = {"from": old, "to": value}
            setattr(obj, name, value)
    return diff

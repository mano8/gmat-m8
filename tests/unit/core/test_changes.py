"""``apply_changes``: allow-listed updates with an audit diff."""

import pytest

from apps.core.changes import apply_changes
from apps.core.models import DocumentSequence


def test_returns_only_real_changes() -> None:
    sequence = DocumentSequence(scope="A", next_value=1)
    diff = apply_changes(sequence, {"scope": "A", "next_value": 5}, {"scope", "next_value"})
    assert diff == {"next_value": {"from": 1, "to": 5}}
    assert sequence.next_value == 5


def test_refuses_fields_outside_the_allow_list() -> None:
    sequence = DocumentSequence(scope="A")
    with pytest.raises(ValueError, match=r"\['next_value'\]"):
        apply_changes(sequence, {"next_value": 2}, {"scope"})
    assert sequence.next_value == 1

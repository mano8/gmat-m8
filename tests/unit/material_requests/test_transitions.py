"""The request state machine (plan §4.3): every allowed and forbidden pair."""

from itertools import product

import pytest
from django.core.exceptions import ValidationError

from apps.material_requests.models import MaterialRequest
from apps.material_requests.transitions import (
    ALLOWED_TRANSITIONS,
    FINAL_STATES,
    is_allowed,
    transition,
)
from tests.support import error_codes

S = MaterialRequest.Status

# The plan's diagram, written out independently of the table it checks.
PLAN: dict[tuple[str, str], set[str]] = {
    (S.DRAFT, S.SUBMITTED): {"submit"},
    (S.DRAFT, S.CANCELLED): {"discard"},
    (S.SUBMITTED, S.DRAFT): {"withdraw", "return_to_draft"},
    (S.SUBMITTED, S.APPROVED): {"finalize"},
    (S.SUBMITTED, S.REJECTED): {"finalize"},
    (S.SUBMITTED, S.CANCELLED): {"cancel"},
    (S.APPROVED, S.DRAFT): {"return_to_draft"},
    (S.APPROVED, S.CANCELLED): {"cancel"},
    (S.APPROVED, S.CLOSED): {"auto_close", "manual_close"},
    (S.CLOSED, S.APPROVED): {"auto_reopen", "reopen"},
}
OPERATIONS = sorted(set().union(*PLAN.values()))
PAIRS = list(product(S.values, S.values))


def test_the_table_is_the_plan() -> None:
    assert {pair: set(ops) for pair, ops in ALLOWED_TRANSITIONS.items()} == PLAN


@pytest.mark.parametrize(("current", "target"), PAIRS)
def test_every_pair(current: str, target: str) -> None:
    request = MaterialRequest(status=current)
    allowed = PLAN.get((current, target), set())
    assert is_allowed(current, target) is bool(allowed)
    for operation in OPERATIONS:
        candidate = MaterialRequest(status=current)
        if operation in allowed:
            assert transition(candidate, target, operation) == current
            assert candidate.status == target
        else:
            with pytest.raises(ValidationError) as exc:
                transition(candidate, target, operation)
            assert error_codes(exc) == {"transition"}
            assert candidate.status == current
    assert request.status == current


def test_final_states_have_no_way_out() -> None:
    for state, target in product(FINAL_STATES, S.values):
        assert not is_allowed(state, target)


def test_no_state_moves_to_itself() -> None:
    assert not any(is_allowed(state, state) for state in S.values)

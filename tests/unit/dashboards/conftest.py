"""Fixtures of the dashboard tests: the purchasing and receiving fixtures (approved request
lines, orders and receipts through the services) and a context per person."""

import pytest

from apps.access.context import AccessContext
from tests.org import People
from tests.unit.purchasing.conftest import (  # noqa: F401 - fixtures used by these tests
    breaker,
    buy,
    cable,
    make_line,
    mm,
)
from tests.unit.receiving.conftest import receive, third  # noqa: F401 - fixtures


@pytest.fixture
def ctx(people: People) -> AccessContext:
    """A-MAD's material manager: buys for A-MAD and B-MAD."""
    return AccessContext.for_user(people.mm_mad)


@pytest.fixture
def other(people: People) -> AccessContext:
    """A-SEV's material manager: sees nothing of A-MAD's documents."""
    return AccessContext.for_user(people.mm_sev)

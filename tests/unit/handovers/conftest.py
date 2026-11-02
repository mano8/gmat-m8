"""Fixtures of the handover tests: the purchasing and receiving fixtures (approved request
lines, orders and receipts through the services) and ``hand_over``, which posts a
``FROM_WAREHOUSE`` handover through the service.

By default the material manager of A-MAD hands material from A-MAD's warehouse over to the
hospital (bought by A-MAD), for the hospital's partner to take.
"""

from collections.abc import Callable
from decimal import Decimal
from typing import Any

import pytest

from apps.access.principals import SessionPrincipal
from apps.handovers import services
from apps.handovers.models import Handover
from apps.handovers.services import HandoverLineInput, Receiver
from apps.locations.models import Location
from apps.material_requests.models import MaterialRequestLine
from tests.org import Org, People
from tests.unit.purchasing.conftest import (  # noqa: F401 - fixtures used by these tests
    breaker,
    buy,
    cable,
    make_line,
    mm,
)
from tests.unit.receiving.conftest import receive, third  # noqa: F401 - fixtures

D = Decimal

type Item = HandoverLineInput | tuple[MaterialRequestLine, str]
type HandOver = Callable[..., Handover]


def as_input(item: Item) -> HandoverLineInput:
    if isinstance(item, HandoverLineInput):
        return item
    line, qty = item
    return HandoverLineInput(line.pk, D(qty))


@pytest.fixture
def hand_over(org: Org, people: People, mm: SessionPrincipal) -> HandOver:  # noqa: F811
    """``hand_over((line, "4"), …)``: a handover of the first line's project."""

    def make(
        *items: Item,
        source: Location | None = None,
        principal: SessionPrincipal | None = None,
        receiver: Receiver | None = None,
        project: Any = None,
        **kwargs: Any,
    ) -> Handover:
        inputs = [as_input(item) for item in items]
        first = MaterialRequestLine.objects.get(pk=inputs[0].request_line_id)
        return services.create_handover(
            principal or mm,
            (project or first.project).pk,
            source_location_id=(source or org.warehouses["A Madrid"]).pk,
            lines=inputs,
            receiver=receiver or Receiver(user_id=people.partner.pk),
            **kwargs,
        )

    return make

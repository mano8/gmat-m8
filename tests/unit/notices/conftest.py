"""Fixtures of the notification tests: approved request lines, orders, receipts and
handovers through the services (purchasing, receiving and handover fixtures) and requests
in any state (``make_request``)."""

from tests.unit.handovers.conftest import hand_over  # noqa: F401 - fixtures used by these tests
from tests.unit.material_requests.conftest import make_request  # noqa: F401
from tests.unit.purchasing.conftest import breaker, buy, cable, make_line, mm  # noqa: F401
from tests.unit.receiving.conftest import receive, third  # noqa: F401

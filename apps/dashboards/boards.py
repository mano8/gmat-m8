"""The boards a dashboard panel can be on (plan M14.3, Q81).

``HOME`` is the day-to-day work every signed-in person opens first; ``ADMINISTRATION`` is the
organisation's own overview, a page of its own for who holds ``org.manage``. A panel is on
exactly one board and is never offered on the other, so moving a panel never takes it from
someone who held it: every ``ADMINISTRATION`` panel reads with ``org.manage``, the capability
of the board itself (``tests/unit/dashboards/test_panels.py`` keeps that true).
"""

from enum import StrEnum


class Board(StrEnum):
    HOME = "home"
    ADMINISTRATION = "administration"

"""The dashboard's key figures (plan M8b.5): at most four tiles for the user's role, each a
count of the list it links to, a cursor count stopping at the cap, and the median time to a
decision compared with the period before."""

from collections.abc import Iterator
from datetime import datetime, timedelta
from typing import Any

import pytest
from django.utils import timezone, translation

from apps.access.context import AccessContext
from apps.dashboards import tiles
from apps.dashboards.period import Period
from apps.material_requests.models import MaterialRequest
from apps.purchasing.models import PurchaseOrder
from tests.org import People
from tests.unit.dashboards.conftest import Requests
from tests.unit.purchasing.conftest import Buy, MakeLine
from tests.unit.receiving.conftest import Receive

pytestmark = pytest.mark.django_db

Status = MaterialRequest.Status
HOUR, DAY = timedelta(hours=1), timedelta(days=1)


@pytest.fixture(autouse=True)
def english() -> Iterator[None]:
    with translation.override("en"):
        yield


def keys(user: Any) -> list[str]:
    return [
        tile.key for tile in tiles.tiles(AccessContext.for_user(user), Period(12, timezone.now()))
    ]


def by_key(user: Any, now: datetime | None = None) -> dict[str, tiles.Tile]:
    found = tiles.tiles(AccessContext.for_user(user), Period(12, now or timezone.now()))
    return {tile.key: tile for tile in found}


class TestRanking:
    def test_each_role_gets_its_first_four(self, people: People) -> None:
        assert keys(people.tech_mad) == ["drafts", "submitted", "approved", "closed"]
        assert keys(people.partner) == ["drafts", "submitted", "approved", "closed"]
        assert keys(people.mm_mad) == ["inbox", "decision-time", "to-purchase", "overdue"]
        assert keys(people.wo_mad) == ["overdue", "awaiting-receipt", "to-hand-over"]
        assert keys(people.site_manager) == ["inbox", "decision-time", "drafts", "submitted"]
        assert keys(people.nobody) == []


class TestValues:
    def test_a_requester_counts_their_own_requests_with_filtered_links(
        self, people: People, requests: Requests
    ) -> None:
        requests.make(status=Status.DRAFT)
        requests.make(status=Status.SUBMITTED)
        requests.make(status=Status.SUBMITTED, by=people.partner)
        found = by_key(people.tech_mad)
        assert (found["drafts"].value, found["submitted"].value) == ("1", "1")
        assert found["drafts"].href == "/requests/?status=DRAFT"
        assert found["closed"].note == "in the last 30 days"

    def test_the_inbox_says_how_many_wait_more_than_a_week(
        self, people: People, requests: Requests
    ) -> None:
        now = timezone.now()
        requests.make(submitted=now - 9 * DAY)
        requests.make(submitted=now - HOUR)
        inbox = by_key(people.mm_mad)["inbox"]
        assert (inbox.value, inbox.note, inbox.href) == (
            "2",
            "1 for more than a week",
            "/requests/inbox/",
        )

    def test_no_decision_yet(self, people: People) -> None:
        tile = by_key(people.mm_mad)["decision-time"]
        assert (tile.value, tile.change, tile.spark) == ("None yet", "", ())

    def test_a_median_with_nothing_before_it(self, people: People, requests: Requests) -> None:
        now = timezone.now()
        requests.make(status=Status.APPROVED, submitted=now - 5 * HOUR, decided=now - 2 * HOUR)
        tile = by_key(people.mm_mad, now)["decision-time"]
        assert tile.value == "3 h"
        assert (tile.change, tile.direction, tile.good) == (
            "Nothing to compare with yet",
            "flat",
            None,
        )
        assert tile.spark == (3.0,)

    @pytest.mark.parametrize(
        ("before", "change", "direction", "good"),
        [
            (13, "10 h faster than the 12 weeks before", "down", True),
            (1, "2 h slower than the 12 weeks before", "up", False),
            (3, "As in the 12 weeks before", "flat", None),
        ],
    )
    def test_the_median_against_the_period_before(
        self,
        people: People,
        requests: Requests,
        before: int,
        change: str,
        direction: str,
        good: bool | None,
    ) -> None:
        now = timezone.now()
        requests.make(status=Status.APPROVED, submitted=now - 5 * HOUR, decided=now - 2 * HOUR)
        old = now - 100 * DAY
        requests.make(status=Status.APPROVED, submitted=old - before * HOUR, decided=old)
        tile = by_key(people.mm_mad, now)["decision-time"]
        assert (tile.change, tile.direction, tile.good) == (change, direction, good)

    def test_to_purchase_and_to_hand_over_stop_at_the_cap(
        self,
        monkeypatch: pytest.MonkeyPatch,
        people: People,
        make_line: MakeLine,
        buy: Buy,
        receive: Receive,
    ) -> None:
        monkeypatch.setattr(tiles, "COUNT_CAP", 1)
        make_line("10")
        make_line("10")
        for _ in range(2):
            receive(buy((make_line("10"), "10")), "6")
        assert by_key(people.mm_mad)["to-purchase"].value == "1+"
        assert by_key(people.wo_mad)["to-hand-over"].value == "1+"
        monkeypatch.setattr(tiles, "COUNT_CAP", 99)
        assert by_key(people.mm_mad)["to-purchase"].value == "2"

    def test_overdue_and_awaiting_receipt(
        self, people: People, make_line: MakeLine, buy: Buy
    ) -> None:
        assert by_key(people.wo_mad)["overdue"].note == "none past its expected date"
        late = buy((make_line("10"), "10"))
        buy((make_line("10"), "10"))
        PurchaseOrder.objects.filter(pk=late.pk).update(
            expected_date=timezone.localdate() - timedelta(days=4)
        )
        found = by_key(people.wo_mad)
        assert found["overdue"].value == "1"
        assert found["overdue"].note.startswith("expected since ")
        assert found["overdue"].href == "/purchasing/orders/?status=ISSUED"
        assert found["awaiting-receipt"].value == "2"


@pytest.mark.parametrize(
    ("value", "words"),
    [
        (timedelta(seconds=10), "1 min"),
        (timedelta(minutes=42), "42 min"),
        (timedelta(hours=30), "30 h"),
        (timedelta(days=3), "3 d"),
        (timedelta(days=2, hours=12), "2.5 d"),
    ],
    ids=["seconds", "minutes", "hours", "days", "half-days"],
)
def test_a_duration_in_short_words(value: timedelta, words: str) -> None:
    with translation.override("en"):
        assert tiles.duration(value) == words

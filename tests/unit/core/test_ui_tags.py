from decimal import Decimal

import pytest
from django.utils import translation

from apps.core.templatetags.ui_tags import at, quantity


class TestAt:
    def test_returns_the_item(self) -> None:
        assert at(["a", "b"], 1) == "b"

    @pytest.mark.parametrize(("items", "index"), [(["a"], 3), (None, 0)])
    def test_an_empty_string_when_there_is_none(self, items: object, index: int) -> None:
        assert at(items, index) == ""  # type: ignore[arg-type]


class TestQuantity:
    @pytest.mark.parametrize(
        ("value", "shown"),
        [(Decimal("10.000"), "10"), (Decimal("2.500"), "2.5"), (Decimal("0.125"), "0.125")],
    )
    def test_drops_trailing_zeros(self, value: Decimal, shown: str) -> None:
        with translation.override("en"):
            assert quantity(value) == shown

    def test_uses_the_decimal_comma_in_spanish(self) -> None:
        with translation.override("es"):
            assert quantity(Decimal("2.500")) == "2,5"

    def test_nothing_for_none(self) -> None:
        assert quantity(None) == ""

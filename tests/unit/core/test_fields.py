"""Quantity and price field presets (apps.core.fields)."""

from decimal import Decimal

import pytest
from django.core.exceptions import ValidationError
from django.db import connection

from apps.core.fields import PriceField, QuantityField


def test_quantity_preset() -> None:
    field = QuantityField()
    assert (field.max_digits, field.decimal_places) == (12, 3)


def test_price_preset() -> None:
    field = PriceField()
    assert (field.max_digits, field.decimal_places) == (14, 4)


def test_precision_cannot_be_overridden() -> None:
    field = QuantityField(max_digits=5, decimal_places=1)
    assert (field.max_digits, field.decimal_places) == (12, 3)


def test_migrations_do_not_repeat_the_preset() -> None:
    _, path, _, kwargs = QuantityField(null=True).deconstruct()
    assert path == "apps.core.fields.QuantityField"
    assert kwargs == {"null": True}


@pytest.mark.parametrize("field", [QuantityField(), PriceField()])
def test_floats_are_refused(field: QuantityField) -> None:
    with pytest.raises(TypeError, match="floats"):
        field.to_python(0.1)
    with pytest.raises(TypeError):
        field.get_db_prep_save(0.1, connection=connection)


def test_decimals_and_text_are_accepted() -> None:
    field = QuantityField()
    assert field.to_python("1.5") == Decimal("1.5")
    assert field.to_python(Decimal("2.125")) == Decimal("2.125")


def test_too_many_places_fail_validation() -> None:
    with pytest.raises(ValidationError):
        QuantityField().clean(Decimal("1.0001"), None)

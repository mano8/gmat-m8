"""Field presets for quantities and prices (plan §4.2, §4.5).

Quantities are ``Decimal(12,3)`` and unit prices ``Decimal(14,4)``; totals are computed,
never stored. A float never reaches the database: the presets refuse one outright instead
of rounding it through binary floating point.
"""

from decimal import Decimal
from typing import Any

from django.db import models

QUANTITY_MAX_DIGITS = 12
QUANTITY_DECIMAL_PLACES = 3
PRICE_MAX_DIGITS = 14
PRICE_DECIMAL_PLACES = 4

QUANTITY_ZERO = Decimal("0.000")
PRICE_ZERO = Decimal("0.0000")


class _FixedDecimalField(models.DecimalField):  # type: ignore[type-arg]
    """A ``DecimalField`` with fixed precision that refuses floats."""

    max_digits_preset: int
    decimal_places_preset: int

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        kwargs["max_digits"] = self.max_digits_preset
        kwargs["decimal_places"] = self.decimal_places_preset
        super().__init__(*args, **kwargs)

    def deconstruct(self) -> Any:
        name, path, args, kwargs = super().deconstruct()
        kwargs.pop("max_digits", None)
        kwargs.pop("decimal_places", None)
        return name, path, args, kwargs

    def to_python(self, value: Any) -> Any:
        if isinstance(value, float):
            raise TypeError(f"{type(self).__name__} does not accept floats; pass a Decimal")
        return super().to_python(value)


class QuantityField(_FixedDecimalField):
    """A quantity: ``Decimal(12,3)``."""

    max_digits_preset = QUANTITY_MAX_DIGITS
    decimal_places_preset = QUANTITY_DECIMAL_PLACES


class PriceField(_FixedDecimalField):
    """A unit price: ``Decimal(14,4)``."""

    max_digits_preset = PRICE_MAX_DIGITS
    decimal_places_preset = PRICE_DECIMAL_PLACES

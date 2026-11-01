"""The quantity rule shared by every document that counts a material (plan §4.2).

A quantity is a ``Decimal`` with no more decimals than its unit allows (``UD`` none, ``M``
two…); request lines, purchase-order lines and allocations all check it before a write.
"""

from decimal import Decimal
from typing import Any

from django.core.exceptions import ValidationError
from django.utils.translation import gettext_lazy as _

from apps.catalog.models import UnitOfMeasure


def check_quantity(quantity: Any, unit: UnitOfMeasure, field: str, *, zero: bool = False) -> None:
    """A positive ``Decimal`` (or zero, with ``zero``) with no more decimals than the unit
    allows; a ``ValidationError`` on ``field`` otherwise."""
    if not isinstance(quantity, Decimal) or not quantity.is_finite():
        raise ValidationError({field: ValidationError(_("Enter a quantity."), code="invalid")})
    if quantity < 0 or (quantity == 0 and not zero):
        raise ValidationError(
            {field: ValidationError(_("The quantity must be positive."), code="positive")}
        )
    exponent = quantity.normalize().as_tuple().exponent
    if isinstance(exponent, int) and -exponent > unit.decimals:
        raise ValidationError(
            {
                field: ValidationError(
                    _("%(unit)s quantities have at most %(decimals)s decimals."),
                    code="precision",
                    params={"unit": unit.code, "decimals": unit.decimals},
                )
            }
        )

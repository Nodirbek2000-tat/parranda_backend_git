from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import extend_schema_field
from rest_framework import serializers

from .ids import fits_db_int

_SIX_PLACES = Decimal("0.000001")


@extend_schema_field(OpenApiTypes.DOUBLE)
class CoordinateField(serializers.Field):
    """Latitude/longitude: accepts any JSON number, stores 6 decimals, returns a number."""

    default_error_messages = {
        "invalid": "Koordinata son bo‘lishi kerak.",
        "range": "Koordinata {low} va {high} oralig‘ida bo‘lishi kerak.",
    }

    def __init__(self, *, low: int, high: int, **kwargs) -> None:
        self.low = Decimal(low)
        self.high = Decimal(high)
        super().__init__(**kwargs)

    def to_internal_value(self, data) -> Decimal:
        if isinstance(data, bool):
            self.fail("invalid")
        try:
            value = Decimal(str(data)).quantize(_SIX_PLACES, rounding=ROUND_HALF_UP)
        except (InvalidOperation, ValueError, TypeError):
            self.fail("invalid")
        if not value.is_finite():
            self.fail("invalid")
        if not self.low <= value <= self.high:
            self.fail("range", low=self.low, high=self.high)
        return value

    def to_representation(self, value) -> float | None:
        return None if value is None else float(value)


def latitude_field(**kwargs) -> CoordinateField:
    return CoordinateField(low=-90, high=90, **kwargs)


def longitude_field(**kwargs) -> CoordinateField:
    return CoordinateField(low=-180, high=180, **kwargs)


class PrimaryKeyField(serializers.PrimaryKeyRelatedField):
    """``PrimaryKeyRelatedField`` that rejects booleans, fractions and out-of-range ids.

    DRF would look up ``true`` as pk 1 and ``1.5`` as pk 1, and pass a 30-digit id
    straight to the database driver (``OverflowError`` → HTTP 500).
    """

    def to_internal_value(self, data):
        if isinstance(data, bool) or (isinstance(data, float) and not data.is_integer()):
            self.fail("incorrect_type", data_type=type(data).__name__)
        if isinstance(data, (int, float, str)):
            try:
                number = int(data)
            except ValueError:
                number = None
            if number is not None and not fits_db_int(number):
                self.fail("does_not_exist", pk_value=data)
        return super().to_internal_value(data)

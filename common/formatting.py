"""Formatting helpers for Telegram texts (``1 500``, ``04.11.2026``)."""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal

from django.utils import timezone


def fmt_number(value: int | float | Decimal | None) -> str:
    """Thousands separated by a space, trailing fractional zeros removed: ``1 500``, ``310.5``."""
    if value is None:
        return "—"
    number = Decimal(str(value))
    sign = "-" if number < 0 else ""
    number = abs(number)
    integer_part = int(number)
    fraction = number - integer_part
    text = f"{integer_part:,}".replace(",", " ")
    if fraction:
        fraction_text = format(fraction.normalize(), "f").split(".")[1]
        text = f"{text}.{fraction_text}"
    return f"{sign}{text}"


def fmt_date(value: date | datetime) -> str:
    if isinstance(value, datetime):
        value = timezone.localtime(value).date()
    return value.strftime("%d.%m.%Y")

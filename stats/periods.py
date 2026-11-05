"""Period query parameters of the stats endpoints (API_CONTRACT_V2 §1.1, §8.1)."""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any

from rest_framework.exceptions import ValidationError

MIN_DATE = date(2000, 1, 1)
MAX_DAYS = 366
DEFAULT_DAYS = 30
DEFAULT_MORTALITY_THRESHOLD = Decimal("5")

MSG_FORMAT = "Sana noto‘g‘ri formatda (YYYY-MM-DD)."
MSG_FUTURE = "Sana kelajakda bo‘lishi mumkin emas"
MSG_ORDER = "Boshlanish sanasi tugash sanasidan keyin bo‘lishi mumkin emas."
MSG_MIN_DATE = "Sana 01.01.2000 dan oldin bo‘lishi mumkin emas."
MSG_TOO_LONG = "Davr 366 kundan oshmasligi kerak."
MSG_THRESHOLD = "0 dan 100 gacha son kiriting."

_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$", re.ASCII)


@dataclass(frozen=True)
class Period:
    """Inclusive calendar period ``date_from … date_to`` and its previous period of the same length."""

    date_from: date
    date_to: date

    @property
    def days(self) -> int:
        return (self.date_to - self.date_from).days + 1

    @property
    def previous_to(self) -> date:
        return self.date_from - timedelta(days=1)

    @property
    def previous_from(self) -> date:
        return self.date_from - timedelta(days=self.days)

    def dates(self) -> list[date]:
        return [self.date_from + timedelta(days=offset) for offset in range(self.days)]

    def as_dict(self, *, previous: bool = True) -> dict[str, Any]:
        data: dict[str, Any] = {
            "date_from": self.date_from.isoformat(),
            "date_to": self.date_to.isoformat(),
            "days": self.days,
        }
        if previous:
            data["previous_from"] = self.previous_from.isoformat()
            data["previous_to"] = self.previous_to.isoformat()
        return data


def days_before(day: date, days: int) -> date:
    """``day − days`` clamped at ``date.min`` (e.g. ``?date_to=0001-01-05``); validation then rejects it."""
    try:
        return day - timedelta(days=days)
    except OverflowError:
        return date.min


def parse_iso_date(raw: str) -> date | None:
    """Strict ``YYYY-MM-DD`` (``None`` when malformed or not a real calendar date)."""
    if not _DATE_RE.match(raw):
        return None
    try:
        return date.fromisoformat(raw)
    except ValueError:
        return None


def _raw(params: Mapping[str, Any], name: str) -> str:
    value = params.get(name)
    return value.strip() if isinstance(value, str) else ""


def parse_dates(params: Mapping[str, Any], names: tuple[str, ...] = ("date_from", "date_to")) -> dict[str, date | None]:
    """Format check of the date parameters (all fields reported together)."""
    values: dict[str, date | None] = {}
    errors: dict[str, list[str]] = {}
    for name in names:
        raw = _raw(params, name)
        if not raw:
            values[name] = None
            continue
        parsed = parse_iso_date(raw)
        if parsed is None:
            errors[name] = [MSG_FORMAT]
        values[name] = parsed
    if errors:
        raise ValidationError(errors)
    return values


def validate_period(date_from: date, date_to: date, today: date, *, max_days: int | None = MAX_DAYS) -> None:
    """Checks of §8.1 in contract order; the first failure wins per field."""
    errors: dict[str, list[str]] = {}
    if date_to > today:
        errors["date_to"] = [MSG_FUTURE]
    if date_from > date_to:
        errors["date_from"] = [MSG_ORDER]
    elif date_from < MIN_DATE:
        errors["date_from"] = [MSG_MIN_DATE]
    if errors:
        raise ValidationError(errors)
    if max_days is not None and (date_to - date_from).days + 1 > max_days:
        raise ValidationError({"non_field_errors": [MSG_TOO_LONG]})


def resolve_period(
    params: Mapping[str, Any],
    today: date,
    *,
    default_days: int = DEFAULT_DAYS,
    default_from: Callable[[date], date] | None = None,
) -> Period:
    """Period from ``?date_from=&date_to=`` with the defaults of §1.1.

    ``default_from(date_to)`` overrides the start used when ``date_from`` is missing
    (``/api/batches/{id}/stats`` starts at the batch arrival).
    """
    values = parse_dates(params)
    date_to = values["date_to"] or today
    date_from = values["date_from"]
    if date_from is None:
        date_from = default_from(date_to) if default_from else days_before(date_to, default_days - 1)
    validate_period(date_from, date_to, today)
    return Period(date_from, date_to)


#: Plain decimal literal with at most one decimal place (``5``, ``2.5``, ``100.0``). Exponent forms such as
#: ``1e-999999999`` are rejected *before* ``Decimal()`` so the cost of later arithmetic never depends on the
#: input; one decimal keeps the echoed ``threshold_pct`` (1 dec) identical to the value compared against.
_THRESHOLD_RE = re.compile(r"^\d{1,3}(?:\.\d)?$", re.ASCII)
_THRESHOLD_QUANTUM = Decimal("0.1")


def parse_threshold(params: Mapping[str, Any], name: str = "mortality_threshold") -> Decimal:
    """``mortality_threshold``: a number ``0 … 100`` with at most one decimal (default ``5``)."""
    raw = _raw(params, name)
    if not raw:
        return DEFAULT_MORTALITY_THRESHOLD
    if not _THRESHOLD_RE.match(raw):
        raise ValidationError({name: [MSG_THRESHOLD]})
    try:
        value = Decimal(raw).quantize(_THRESHOLD_QUANTUM)
    except (InvalidOperation, ValueError):  # pragma: no cover - excluded by the regex
        raise ValidationError({name: [MSG_THRESHOLD]}) from None
    if not Decimal(0) <= value <= Decimal(100):
        raise ValidationError({name: [MSG_THRESHOLD]})
    return value

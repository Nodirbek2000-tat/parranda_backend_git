"""Report building blocks (API_CONTRACT_V3 §11): columns, the built table, value formatting.

A builder returns a :class:`ReportData` (columns, rows, totals, summary). The same object feeds the JSON
preview (§11.3), the Excel file (:mod:`reports.xlsx`) and the PDF (:mod:`reports.pdf`), so the three formats
always show the same numbers.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from django.utils import timezone

from common.formatting import fmt_number

TEXT = "text"
INT = "int"
DECIMAL = "decimal"
PERCENT = "percent"
DATE = "date"
DATETIME = "datetime"
BOOL = "bool"

NUMERIC = frozenset({INT, DECIMAL, PERCENT})
EMPTY = "—"


@dataclass(frozen=True)
class Column:
    key: str
    label: str
    type: str = TEXT
    decimals: int | None = None

    def as_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {"key": self.key, "label": self.label, "type": self.type}
        if self.type in (DECIMAL, PERCENT):
            data["decimals"] = self.decimals if self.decimals is not None else (1 if self.type == PERCENT else 2)
        return data

    @property
    def places(self) -> int:
        if self.decimals is not None:
            return self.decimals
        return 1 if self.type == PERCENT else 2


@dataclass
class ReportData:
    columns: list[Column]
    rows: list[dict[str, Any]]
    totals: dict[str, Any] = field(default_factory=dict)
    summary: list[tuple[str, str]] = field(default_factory=list)


def col(key: str, label: str, kind: str = TEXT, decimals: int | None = None) -> Column:
    return Column(key, label, kind, decimals)


# --- values -------------------------------------------------------------------------------------


def json_value(column: Column, value: Any) -> Any:
    """JSON-native cell value: dates ISO, decimals rounded floats, ``None`` stays ``None``."""
    if value is None:
        return None
    if column.type == DATE:
        return value.isoformat() if isinstance(value, (date, datetime)) else value
    if column.type == DATETIME:
        if isinstance(value, datetime):
            return timezone.localtime(value).replace(microsecond=0).isoformat()
        return value
    if column.type == INT:
        return int(value)
    if column.type in (DECIMAL, PERCENT):
        from stats.metrics import round_half_up

        return round_half_up(value, column.places)
    if column.type == BOOL:
        return bool(value)
    return str(value)


def display_value(column: Column, value: Any) -> str:
    """Human text (PDF cells): ``1 500``, ``82.5%``, ``04.11.2026``, ``Ha``/``Yo‘q``, ``—``."""
    if value is None or value == "":
        return EMPTY if column.type != TEXT else ""
    if column.type == DATE:
        return value.strftime("%d.%m.%Y") if isinstance(value, (date, datetime)) else str(value)
    if column.type == DATETIME:
        if isinstance(value, datetime):
            return timezone.localtime(value).strftime("%d.%m.%Y %H:%M")
        return str(value)
    if column.type == BOOL:
        return "Ha" if value else "Yo‘q"
    if column.type == INT:
        return fmt_number(int(value))
    if column.type in (DECIMAL, PERCENT):
        from stats.metrics import round_half_up

        rounded = round_half_up(value, column.places)
        text = _fixed(rounded, column.places)
        return f"{text}%" if column.type == PERCENT else text
    return str(value)


def _fixed(value: float, places: int) -> str:
    """``1234.5`` → ``1 234.50`` (fixed decimals, space thousands)."""
    text = f"{value:,.{places}f}".replace(",", " ")
    return text


def to_decimal(value: Any) -> Decimal | None:
    if value is None:
        return None
    return value if isinstance(value, Decimal) else Decimal(str(value))

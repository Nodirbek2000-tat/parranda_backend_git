"""Telegram HTML texts in the Phase 1 style (API_CONTRACT §4, V3 §9.4).

Every module builds its text with :func:`compose`::

    rows = [("Buyurtma", order.code), *household_rows(order.household), ("Sana", fmt_date(order.created_at))]
    text = compose("order_created", rows, "Yangi", order.code)

Values are HTML-escaped, empty values (``None`` / ``""``) are skipped.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from django.utils.html import escape

from common.formatting import fmt_number

from . import registry

if TYPE_CHECKING:
    from farm.models import Household

Row = tuple[str, object]

#: Long free texts (conclusions) are cut to this many characters plus ``…``.
MAX_EXCERPT = 300


def compose(event: str, rows: list[Row], status: str, op_id: str) -> str:
    """``<emoji> <b>NNP — TITLE</b>`` + ``Label: value`` rows + ``Holat`` + ``Operatsiya ID``."""
    title = registry.get(event).title_html
    body = "\n".join(f"{label}: {escape(str(value))}" for label, value in rows if value not in (None, ""))
    return f"{title}\n\n{body}\n\nHolat: {escape(status)}\nOperatsiya ID: #{escape(op_id)}"


def household_rows(household: Household) -> list[Row]:
    return [
        ("Xonadon", f"{household.name} ({household.code})"),
        ("Mas’ul", household.owner.full_name),
    ]


def qty(value, unit: str) -> str:
    """``1 500 kg`` (thousands separated by a space)."""
    return f"{fmt_number(value)} {unit}"


def excerpt(text: str, limit: int = MAX_EXCERPT) -> str:
    text = (text or "").strip()
    return text if len(text) <= limit else f"{text[:limit].rstrip()}…"


def items_text(items: list[tuple[str, object, str]], limit: int = 5) -> str:
    """``Yem PK-1 — 150 kg; Vitamin — 2 dona`` (max ``limit`` then ``… va yana 2 ta``)."""
    parts = [f"{name} — {qty(quantity, unit)}" for name, quantity, unit in items[:limit]]
    text = "; ".join(parts)
    if len(items) > limit:
        text = f"{text} … va yana {len(items) - limit} ta"
    return text

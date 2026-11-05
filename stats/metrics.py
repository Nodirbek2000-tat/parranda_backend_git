"""Pure metric formulas of API_CONTRACT_V2 §1.2 (no database access; unit tested by hand-computed numbers).

Conventions
-----------
* Sums are exact integers. Feed amounts are carried in **hundredths of a kg** (``feed_cents``) so that
  every sum stays an exact integer, whatever the database (SQLite stores decimals as REAL).
* Ratios are ``Decimal`` (context precision 28) computed from the exact sums; they are rounded **only
  at output** with ``ROUND_HALF_UP`` (:func:`round_half_up`). Division by zero gives ``None`` (JSON null).
* Deltas are computed from unrounded values.
"""

from __future__ import annotations

from dataclasses import dataclass, field, fields
from datetime import date, timedelta
from decimal import ROUND_HALF_UP, Decimal
from fractions import Fraction
from typing import Any

ZERO = Decimal(0)
HUNDRED = Decimal(100)
CENTS_PER_UNIT = 100
#: ``stockout_date`` is ``null`` when the stock lasts longer than this.
MAX_COVER_DAYS = 3650

Number = int | Decimal | Fraction


def to_decimal(value: Number) -> Decimal:
    if isinstance(value, Fraction):
        return Decimal(value.numerator) / Decimal(value.denominator)
    return Decimal(value)


def round_half_up(value: Number | None, places: int) -> float | None:
    """Half-up rounding to ``places`` decimals; ``None`` stays ``None`` and ``-0.0`` becomes ``0.0``."""
    if value is None:
        return None
    quantum = Decimal(1).scaleb(-places)
    rounded = to_decimal(value).quantize(quantum, rounding=ROUND_HALF_UP)
    result = float(rounded)
    return 0.0 if result == 0 else result


def ratio(numerator: Number, denominator: Number) -> Decimal | None:
    """``numerator / denominator`` as ``Decimal``; ``None`` when the denominator is 0."""
    if not denominator:
        return None
    return to_decimal(numerator) / to_decimal(denominator)


def percent(part: Number, whole: Number) -> Decimal | None:
    value = ratio(part, whole)
    return None if value is None else value * HUNDRED


def cents_to_units(cents: Number) -> Decimal:
    return to_decimal(cents) / CENTS_PER_UNIT


# --- Flock / production formulas ------------------------------------------------------------


def birds_avg(bird_days: int, days: int) -> Decimal | None:
    """Average number of birds: ``Σ_d B(d) / N``."""
    return ratio(bird_days, days)


def laying_rate_pct(layer_eggs: int, layer_bird_days: int) -> Decimal | None:
    """Hen-day laying rate ``100 × Σ_{R_L} eggs / Σ_{R_L} L_h(d)``."""
    return percent(layer_eggs, layer_bird_days)


def feed_per_egg_g(layer_feed_cents: Number, layer_eggs: int) -> Decimal | None:
    """``1000 × Σ_{R_L} feed × L/B / Σ_{R_L} eggs`` (grams of layer feed per egg)."""
    value = ratio(layer_feed_cents, layer_eggs)
    return None if value is None else value * 1000 / CENTS_PER_UNIT


def feed_per_bird_day_kg(feed_cents: int, bird_days: int) -> Decimal | None:
    """``Σ_{R+} feed / Σ_{R+} B_h(d)`` (kg per bird per day)."""
    value = ratio(feed_cents, bird_days)
    return None if value is None else value / CENTS_PER_UNIT


def mortality_pct(deaths: int, bird_days: int, days: int) -> Decimal | None:
    """``100 × deaths / birds_avg`` with ``birds_avg = bird_days / N`` (``None`` when ``birds_avg = 0``)."""
    if not bird_days or not days:
        return None
    return HUNDRED * deaths * days / Decimal(bird_days)


def mortality_fraction(deaths: int, bird_days: int, days: int) -> Fraction | None:
    """Exact mortality (for threshold comparisons and ordering)."""
    if not bird_days or not days:
        return None
    return Fraction(100 * deaths * days, bird_days)


def discipline_pct(reported: int, expected: int) -> Decimal | None:
    return percent(reported, expected)


def layer_feed_share_cents(feed_cents: int, layer_birds: int, birds: int) -> Decimal:
    """Feed allocated to layers on one report day: ``feed × L/B`` (``feed`` when the flock is all layers).

    Only called with ``L > 0``. Legacy back-dated DEATH/OUT movements (Phase 1 validated them only against
    the *current* count; the service now checks every later end-of-day count) can make another poultry type
    negative on a past day, so ``B ≤ L`` (even ``B = 0``) is possible in old data; the share is then capped
    at 1 (all feed to layers) instead of dividing by zero or allocating more feed than was reported.
    """
    if birds <= layer_birds:
        return Decimal(feed_cents)
    return Decimal(feed_cents) * layer_birds / Decimal(birds)


# --- Deltas and KPI objects ------------------------------------------------------------------

DELTA_RELATIVE = "%"
DELTA_POINTS = "pp"


def delta_relative(value: Number | None, previous: Number | None) -> Decimal | None:
    """``100 × (value − previous) / previous``; ``None`` if either is ``None`` or ``previous = 0``."""
    if value is None or previous is None or not previous:
        return None
    return HUNDRED * (to_decimal(value) - to_decimal(previous)) / to_decimal(previous)


def delta_points(value: Number | None, previous: Number | None) -> Decimal | None:
    """Percentage points ``value − previous``; ``None`` if either is ``None``."""
    if value is None or previous is None:
        return None
    return to_decimal(value) - to_decimal(previous)


def output(value: Number | None, places: int | None) -> int | float | None:
    """JSON value: ``places=None`` → exact integer, else half-up rounded float."""
    if value is None:
        return None
    if places is None:
        return int(value)
    return round_half_up(value, places)


def kpi(value: Number | None, previous: Number | None, *, places: int | None, unit: str) -> dict[str, Any]:
    delta = delta_points(value, previous) if unit == DELTA_POINTS else delta_relative(value, previous)
    return {
        "value": output(value, places),
        "previous": output(previous, places),
        "delta": round_half_up(delta, 1),
        "delta_unit": unit,
    }


@dataclass(slots=True)
class Totals:
    """Exact aggregates of a set of households over a period (inputs of every §1.2 formula).

    ``rl_*`` = report days with layer birds (``R_L``), ``rp_*`` = report days with birds (``R+``).
    """

    bird_days: int = 0
    birds_end: int = 0
    eggs: int = 0
    feed_cents: int = 0
    deaths: int = 0
    distributed: int = 0
    returned: int = 0
    reports: int = 0
    rl_eggs: int = 0
    rl_layer_days: int = 0
    rl_reports: int = 0
    rl_feed_cents: Decimal = field(default_factory=lambda: ZERO)
    rp_feed_cents: int = 0
    rp_bird_days: int = 0
    expected: int = 0
    reported_expected: int = 0

    def add(self, other: Totals) -> Totals:
        for item in fields(self):
            setattr(self, item.name, getattr(self, item.name) + getattr(other, item.name))
        return self

    @classmethod
    def merged(cls, items) -> Totals:
        total = cls()
        for item in items:
            total.add(item)
        return total


#: KPI key → (output decimals or None for integers, delta unit). Order = response order.
KPI_SPEC: dict[str, tuple[int | None, str]] = {
    "birds_end": (None, DELTA_RELATIVE),
    "birds_avg": (1, DELTA_RELATIVE),
    "eggs": (None, DELTA_RELATIVE),
    "feed_kg": (2, DELTA_RELATIVE),
    "deaths": (None, DELTA_RELATIVE),
    "distributed": (None, DELTA_RELATIVE),
    "returned": (None, DELTA_RELATIVE),
    "reports": (None, DELTA_RELATIVE),
    "laying_rate_pct": (1, DELTA_POINTS),
    "feed_per_egg_g": (1, DELTA_RELATIVE),
    "feed_per_bird_day_kg": (3, DELTA_RELATIVE),
    "mortality_pct": (1, DELTA_POINTS),
    "report_discipline_pct": (1, DELTA_POINTS),
}


def kpi_values(totals: Totals, days: int) -> dict[str, Number | None]:
    """Unrounded value of every KPI of §1.2 for ``totals`` over ``days`` calendar days."""
    return {
        "birds_end": totals.birds_end,
        "birds_avg": birds_avg(totals.bird_days, days),
        "eggs": totals.eggs,
        "feed_kg": cents_to_units(totals.feed_cents),
        "deaths": totals.deaths,
        "distributed": totals.distributed,
        "returned": totals.returned,
        "reports": totals.reports,
        "laying_rate_pct": laying_rate_pct(totals.rl_eggs, totals.rl_layer_days),
        "feed_per_egg_g": feed_per_egg_g(totals.rl_feed_cents, totals.rl_eggs),
        "feed_per_bird_day_kg": feed_per_bird_day_kg(totals.rp_feed_cents, totals.rp_bird_days),
        "mortality_pct": mortality_pct(totals.deaths, totals.bird_days, days),
        "report_discipline_pct": discipline_pct(totals.reported_expected, totals.expected),
    }


def kpis(current: Totals, previous: Totals, days: int) -> dict[str, dict[str, Any]]:
    """The 13 KPI objects ``{value, previous, delta, delta_unit}`` (both periods have ``days`` days)."""
    now = kpi_values(current, days)
    before = kpi_values(previous, days)
    return {key: kpi(now[key], before[key], places=places, unit=unit) for key, (places, unit) in KPI_SPEC.items()}


# --- Stock formulas --------------------------------------------------------------------------


def effective_days(date_from: date, date_to: date, active_from: date | None) -> int:
    """``N_eff``: calendar days of the period on which the product (or group) already existed.

    ``active_from`` = local day the product appeared (``min(created_at day, first transaction day)``; for a
    group the earliest of its products). ``N_eff = (date_to − max(date_from, active_from)).days + 1``,
    clamped to ``1 … N``; ``N`` when ``active_from`` is unknown or after ``date_to`` (then nothing went out).
    A product created 1 day ago with 150 kg out has 150 kg/day, not 150 / 30 = 5 kg/day.
    """
    days = (date_to - date_from).days + 1
    if active_from is None or active_from <= date_from or active_from > date_to:
        return days
    return (date_to - active_from).days + 1


def avg_daily_out(out_qty: Number, days: int) -> Decimal | None:
    """``out_qty / N_eff`` (calendar days since the product existed, also days without OUT)."""
    return ratio(out_qty, days)


def days_of_cover(balance: Number, daily_out: Decimal | None) -> Decimal | None:
    """Current balance divided by the average daily OUT (``None`` when nothing goes out)."""
    if daily_out is None or not daily_out:
        return None
    return to_decimal(balance) / daily_out


def stockout_date(today: date, cover: Decimal | None) -> date | None:
    """``today + floor(days_of_cover)`` days; ``None`` beyond :data:`MAX_COVER_DAYS` days."""
    if cover is None or cover > MAX_COVER_DAYS:
        return None
    return today + timedelta(days=int(cover.to_integral_value(rounding="ROUND_FLOOR")))


def share_pct(part: Number, whole: Number) -> Decimal | None:
    return percent(part, whole)

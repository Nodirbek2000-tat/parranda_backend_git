"""One function per stats endpoint (API_CONTRACT_V2 §1.4–§1.9, §5.1).

Every function evaluates ``today`` once, runs a fixed number of aggregate queries (§7.1) and builds the
response with the pure formulas of :mod:`stats.metrics` / :mod:`stats.flock`.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Mapping
from datetime import date, timedelta
from decimal import Decimal
from fractions import Fraction
from typing import Any

from django.db.models import Count, F, Min, Q, Sum
from django.utils import timezone

from accounts.models import User
from common.roles import Role, role_of
from farm import selectors
from farm.models import DailyReport, Household, MovementType, PoultryBatch, PoultryMovement, PoultryType
from farm.services import MSG_NO_HOUSEHOLD
from warehouse.models import Product, ProductCategory, StockTransaction, StockTransactionType, Unit, returned_task_out

from . import metrics, queries
from .flock import POULTRY_TYPES, FlockWindow, HouseholdMeta, ScopeStats
from .metrics import Totals, round_half_up
from .periods import Period, days_before, parse_threshold, resolve_period

TOP_LIMIT = 5
TOP_MIN_REPORTS = 3
HIGH_MORTALITY_LIMIT = 20
CONSUMERS_LIMIT = 10
BATCH_DEFAULT_SPAN = 365
SUMMARY_DAYS = 7

POULTRY_LABELS = dict(PoultryType.choices)
CATEGORY_LABELS = dict(ProductCategory.choices)
CATEGORY_ORDER = {value: position for position, value in enumerate(ProductCategory.values)}
UNIT_ORDER = {value: position for position, value in enumerate(Unit.values)}


class SummaryError(Exception):
    """Bot summary cannot be built for this user (``status`` + Uzbek ``detail``)."""

    def __init__(self, status: int, detail: str) -> None:
        super().__init__(detail)
        self.status = status
        self.detail = detail


def _today(today: date | None) -> date:
    return today or timezone.localdate()


def _kg(cents: int | Decimal) -> float:
    return round_half_up(metrics.cents_to_units(cents), 2)


def _amount(value: Decimal) -> float:
    return round_half_up(value, 2)


# --- household-scope helpers ------------------------------------------------------------------


def _flock_scope(
    period: Period,
    households: list[HouseholdMeta],
    today: date,
    *,
    household_id: int | None = None,
    previous: bool = True,
    poultry_type: str | None = None,
) -> ScopeStats:
    """Three queries (Q-a, Q-b, Q-c) over ``previous_from … date_to`` (or the period only)."""
    days = period.days
    start = period.previous_from if previous else period.date_from
    window = FlockWindow(
        start,
        period.date_to,
        queries.movement_opening(start, household_id),
        queries.movement_daily(start, period.date_to, household_id),
    )
    reports = queries.report_rows(start, period.date_to, household_id)
    if poultry_type:
        # Same household-set rule as the reports (V3 §11.1): households with a movement of that type.
        households = [meta for meta in households if poultry_type in window.types_of(meta.id)]
    offset = days if previous else 0
    return ScopeStats(
        window,
        households,
        reports,
        current=(offset, offset + days - 1),
        previous=(0, days - 1) if previous else None,
        today=today,
    )


def _type_row(poultry_type: str, totals: Totals, days: int) -> dict[str, Any]:
    return {
        "poultry_type": poultry_type,
        "poultry_type_display": POULTRY_LABELS[poultry_type],
        "birds_end": totals.birds_end,
        "birds_avg": round_half_up(metrics.birds_avg(totals.bird_days, days), 1),
        "deaths": totals.deaths,
        "mortality_pct": round_half_up(metrics.mortality_pct(totals.deaths, totals.bird_days, days), 1),
    }


def _day_laying(scope: ScopeStats, j: int) -> float | None:
    return round_half_up(metrics.laying_rate_pct(scope.series.rl_eggs[j], scope.series.rl_layer_days[j]), 1)


# --- §1.4 overview ----------------------------------------------------------------------------


MSG_BAD_CHOICE = "“{value}” ruxsat etilgan qiymat emas."


def overview_filters(params: Mapping[str, Any]) -> dict[str, str]:
    """Optional ``region``, ``district``, ``poultry_type`` of ``/api/stats/overview`` (dashboard filters)."""
    from rest_framework.exceptions import ValidationError

    result: dict[str, str] = {}
    for name in ("region", "district"):
        value = str(params.get(name) or "").strip()
        if value:
            result[name] = value[:128]
    if str(params.get("exclude_demo") or "").strip() in ("1", "true"):
        result["exclude_demo"] = "1"
    poultry_type = str(params.get("poultry_type") or "").strip()
    if poultry_type:
        if poultry_type not in POULTRY_TYPES:
            raise ValidationError({"poultry_type": [MSG_BAD_CHOICE.format(value=poultry_type[:32])]})
        result["poultry_type"] = poultry_type
    return result


def _meat(period: Period, household_ids: list[int] | None) -> dict[str, Any]:
    """Meat output (``DailyReport.meat_kg``) per day of the period + totals of the previous period. One query."""
    qs = DailyReport.objects.filter(date__gte=period.previous_from, date__lte=period.date_to)
    if household_ids is not None:
        qs = qs.filter(household_id__in=household_ids)
    rows = (
        qs.order_by()
        .values("date")
        .annotate(meat=Sum("meat_kg"), weight=Sum("avg_weight_g"), weighed=Count("avg_weight_g"))
        .values_list("date", "meat", "weight", "weighed")
    )
    per_day: dict[date, Decimal] = {}
    current = previous = Decimal(0)
    weight_sum = Decimal(0)
    weighed = 0
    for day, meat, weight, count in rows:
        meat = meat or Decimal(0)
        if day >= period.date_from:
            per_day[day] = meat
            current += meat
            weight_sum += weight or Decimal(0)
            weighed += count
        else:
            previous += meat
    return {
        "per_day": per_day,
        "total_kg": float(round_half_up(current, 2)),
        "previous_total_kg": float(round_half_up(previous, 2)),
        "avg_weight_g": float(round_half_up(weight_sum / weighed, 1)) if weighed else None,
    }


def overview(params: Mapping[str, Any], today: date | None = None) -> dict[str, Any]:
    today = _today(today)
    period = resolve_period(params, today)
    threshold = parse_threshold(params)
    filters = overview_filters(params)
    days = period.days
    household_qs = Household.objects.all()
    if "region" in filters:
        household_qs = household_qs.filter(region=filters["region"])
    if "district" in filters:
        household_qs = household_qs.filter(district=filters["district"])
    if "exclude_demo" in filters:
        household_qs = household_qs.filter(is_demo=False)
    households = queries.household_meta(household_qs)
    scope = _flock_scope(period, households, today, poultry_type=filters.get("poultry_type"))
    meat = _meat(period, list(scope.states) if filters else None)
    center_initial = queries.batches_initial_through(period.date_to)
    first, last = scope.current

    current = scope.total_current()
    states = list(scope.states.values())
    center_deaths, center_out = scope.window.center_between(first, last)

    series = []
    for j, day in enumerate(period.dates()):
        series.append(
            {
                "date": day.isoformat(),
                "birds": scope.series.birds[j],
                "layer_birds": scope.series.layer_birds[j],
                "eggs": scope.series.eggs[j],
                "feed_kg": _kg(scope.series.feed_cents[j]),
                "meat_kg": float(meat["per_day"].get(day, Decimal(0))),
                "deaths": scope.series.deaths[j],
                "distributed": scope.series.distributed[j],
                "returned": scope.series.returned[j],
                "reports": scope.series.reports[j],
                "reported_expected": scope.series.reported_expected[j],
                "expected_reports": scope.series.expected[j],
                "laying_rate_pct": _day_laying(scope, j),
            }
        )

    by_type = []
    for poultry_type in POULTRY_TYPES:
        count, totals = scope.type_totals(poultry_type)
        row = _type_row(poultry_type, totals, days)
        row["households"] = count
        row["distributed"] = totals.distributed
        by_type.append(
            {key: row[key] for key in ("poultry_type", "poultry_type_display", "households", "birds_end",
                                        "birds_avg", "deaths", "distributed", "mortality_pct")}
        )

    return {
        "period": period.as_dict(),
        "generated_at": queries.generated_at(),
        "filters": filters,
        # TZ §4 "tuxum yoki go‘sht": meat output from the daily reports (broilers).
        "meat": {key: meat[key] for key in ("total_kg", "previous_total_kg", "avg_weight_g")},
        "households": {
            "total": len(states),
            "active": sum(1 for state in states if state.meta.is_active),
            "with_birds": sum(1 for state in states if state.current.birds_end > 0),
            "reporting": sum(1 for state in states if state.current.reports > 0),
        },
        "kpis": metrics.kpis(current, scope.total_previous(), days),
        "center": {"birds_end": center_initial + scope.window.center_net, "deaths": center_deaths, "out": center_out},
        "series": series,
        "by_poultry_type": by_type,
        "by_district": _by_district(scope, days),
        **_ranking(scope),
        "high_mortality": _high_mortality(scope, days, threshold),
    }


def _by_district(scope: ScopeStats, days: int) -> list[dict[str, Any]]:
    groups: dict[tuple[str, str], list] = defaultdict(list)
    for state in scope.states.values():
        groups[(state.meta.region, state.meta.district)].append(state)
    rows = []
    for (region, district), states in groups.items():
        totals = Totals.merged(state.current for state in states)
        values = metrics.kpi_values(totals, days)
        rows.append(
            {
                "region": region,
                "district": district,
                "households": len(states),
                "households_active": sum(1 for state in states if state.meta.is_active),
                "birds_end": totals.birds_end,
                "eggs": totals.eggs,
                "feed_kg": _kg(totals.feed_cents),
                "deaths": totals.deaths,
                "reports": totals.reports,
                "laying_rate_pct": round_half_up(values["laying_rate_pct"], 1),
                "mortality_pct": round_half_up(values["mortality_pct"], 1),
                "report_discipline_pct": round_half_up(values["report_discipline_pct"], 1),
            }
        )
    rows.sort(key=lambda row: (-row["birds_end"], row["region"], row["district"]))
    return rows


def _ranking(scope: ScopeStats) -> dict[str, list[dict[str, Any]]]:
    candidates = [
        state
        for state in scope.states.values()
        if state.current.reports >= TOP_MIN_REPORTS and state.current.rl_layer_days > 0
    ]

    def rate(state) -> Fraction:
        return Fraction(state.current.rl_eggs, state.current.rl_layer_days)

    top = sorted(candidates, key=lambda state: (-rate(state), -state.current.eggs, state.meta.id))[:TOP_LIMIT]
    top_ids = {state.meta.id for state in top}
    bottom = [
        state
        for state in sorted(candidates, key=lambda state: (rate(state), state.meta.id))
        if state.meta.id not in top_ids
    ][:TOP_LIMIT]

    def item(state) -> dict[str, Any]:
        totals = state.current
        return {
            "household": state.meta.short(),
            "district": state.meta.district,
            "is_active": state.meta.is_active,
            "laying_rate_pct": round_half_up(metrics.laying_rate_pct(totals.rl_eggs, totals.rl_layer_days), 1),
            "eggs": totals.eggs,
            "reports": totals.reports,
            "layer_birds_avg": round_half_up(metrics.ratio(totals.rl_layer_days, totals.rl_reports), 1),
        }

    return {"top_households": [item(state) for state in top], "bottom_households": [item(state) for state in bottom]}


def _high_mortality(scope: ScopeStats, days: int, threshold: Decimal) -> dict[str, Any]:
    limit = Fraction(threshold)
    matches = []
    for state in scope.states.values():
        totals = state.current
        rate = metrics.mortality_fraction(totals.deaths, totals.bird_days, days)
        if totals.bird_days > 0 and totals.deaths >= 1 and rate is not None and rate > limit:
            matches.append((rate, state))
    matches.sort(key=lambda pair: (-pair[0], pair[1].meta.id))
    return {
        "threshold_pct": round_half_up(threshold, 1),
        "count": len(matches),
        "items": [
            {
                "household": state.meta.short(),
                "district": state.meta.district,
                "is_active": state.meta.is_active,
                "birds_avg": round_half_up(metrics.birds_avg(state.current.bird_days, days), 1),
                "deaths": state.current.deaths,
                "mortality_pct": round_half_up(rate, 1),
            }
            for rate, state in matches[:HIGH_MORTALITY_LIMIT]
        ],
    }


# --- §1.5 / §1.9 household stats --------------------------------------------------------------


def household_stats(household: Household, params: Mapping[str, Any], today: date | None = None) -> dict[str, Any]:
    today = _today(today)
    period = resolve_period(params, today)
    days = period.days
    meta = queries.meta_of(household)
    scope = _flock_scope(period, [meta], today, household_id=household.pk)
    state = scope.states[household.pk]
    current = state.current

    series = []
    missing_days = []
    for j, day in enumerate(period.dates()):
        expected = scope.series.expected[j] > 0
        reported = j in scope.reported_days
        # Today is not over: a due report that does not exist yet is "pending", not missing.
        pending = expected and not reported and day == today
        if expected and not reported and not pending:
            missing_days.append(day.isoformat())
        series.append(
            {
                "date": day.isoformat(),
                "birds": scope.series.birds[j],
                "layer_birds": scope.series.layer_birds[j],
                "eggs": scope.series.eggs[j],
                "feed_kg": _kg(scope.series.feed_cents[j]),
                "deaths": scope.series.deaths[j],
                "distributed": scope.series.distributed[j],
                "returned": scope.series.returned[j],
                "reported": reported,
                "expected": expected,
                "pending": pending,
                "laying_rate_pct": _day_laying(scope, j),
            }
        )

    by_type = []
    for poultry_type in POULTRY_TYPES:
        totals = state.type_totals.get(poultry_type)
        if totals is not None and (totals.birds_end > 0 or totals.deaths > 0):
            by_type.append(_type_row(poultry_type, totals, days))

    return {
        "household": {
            "id": household.pk,
            "code": household.code,
            "name": household.name,
            "is_active": household.is_active,
            "region": household.region,
            "district": household.district,
        },
        "period": period.as_dict(),
        "generated_at": queries.generated_at(),
        "kpis": metrics.kpis(current, state.previous, days),
        "discipline": {
            "expected": current.expected,
            "reported": current.reported_expected,
            "missing": current.expected - current.reported_expected,
        },
        "missing_days": missing_days,
        "series": series,
        "by_poultry_type": by_type,
        "batches": selectors.household_batches(household.pk),
    }


# --- §1.6 batch stats -------------------------------------------------------------------------


def batch_period(batch: PoultryBatch, params: Mapping[str, Any], today: date) -> Period:
    def default_from(date_to: date) -> date:
        return min(date_to, max(batch.arrived_at, days_before(date_to, BATCH_DEFAULT_SPAN)))

    return resolve_period(params, today, default_from=default_from)


def batch_stats(batch: PoultryBatch, batch_data: dict, params: Mapping[str, Any], today: date | None = None):
    today = _today(today)
    period = batch_period(batch, params, today)
    rows = (
        PoultryMovement.objects.filter(batch_id=batch.pk)
        .order_by()
        .values("household_id", "household__name", "household__is_active", "type", "date")
        .annotate(quantity=queries.int_sum("quantity"))
        .values_list("household_id", "household__name", "household__is_active", "type", "date", "quantity")
    )
    first, last = period.date_from, period.date_to
    days = period.days
    distributed = returned = 0
    deaths_center = deaths_households = out_center = out_households = 0
    households: dict[int, dict[str, Any]] = {}
    # Period series: opening values at the end of the day before ``first`` + per-day changes.
    center_open = batch.initial_quantity if batch.arrived_at < first else 0
    households_open = 0
    day_center = [0] * days
    day_households = [0] * days
    day_flows = [[0, 0, 0, 0] for _ in range(days)]  # distributed, returned, deaths, out
    if first <= batch.arrived_at <= last:
        day_center[(batch.arrived_at - first).days] += batch.initial_quantity

    for household_id, name, is_active, movement_type, day, quantity in rows:
        center_delta = household_delta = 0
        if movement_type == MovementType.DISTRIBUTE:
            distributed += quantity
            center_delta, household_delta = -quantity, quantity
        elif movement_type == MovementType.RETURN:
            returned += quantity
            center_delta, household_delta = quantity, -quantity
        elif household_id is None:
            center_delta = -quantity
            if movement_type == MovementType.DEATH:
                deaths_center += quantity
            else:
                out_center += quantity
        else:
            household_delta = -quantity
            if movement_type == MovementType.DEATH:
                deaths_households += quantity
            else:
                out_households += quantity
        if household_id is not None:
            entry = households.setdefault(
                household_id,
                {"id": household_id, "name": name, "is_active": is_active, "count_now": 0, "distributed": 0,
                 "returned": 0, "deaths": 0, "out": 0},
            )
            entry["count_now"] += household_delta
            key = {"DISTRIBUTE": "distributed", "RETURN": "returned", "DEATH": "deaths", "OUT": "out"}[movement_type]
            entry[key] += quantity
        if day < first:
            center_open += center_delta
            households_open += household_delta
        elif day <= last:
            j = (day - first).days
            day_center[j] += center_delta
            day_households[j] += household_delta
            index = {"DISTRIBUTE": 0, "RETURN": 1, "DEATH": 2, "OUT": 3}[movement_type]
            day_flows[j][index] += quantity

    deaths_total = deaths_center + deaths_households
    out_total = out_center + out_households
    center_stock = batch.initial_quantity - distributed + returned - deaths_center - out_center
    in_households = sum(entry["count_now"] for entry in households.values())

    series = []
    center, in_house = center_open, households_open
    for j, day in enumerate(period.dates()):
        center += day_center[j]
        in_house += day_households[j]
        flows = day_flows[j]
        series.append(
            {
                "date": day.isoformat(),
                "center_stock": center,
                "in_households": in_house,
                "distributed": flows[0],
                "returned": flows[1],
                "deaths": flows[2],
                "out": flows[3],
            }
        )

    household_rows = sorted(households.values(), key=lambda entry: (-entry["count_now"], entry["id"]))
    days_since = (today - batch.arrived_at).days
    return {
        "batch": batch_data,
        "generated_at": queries.generated_at(),
        "period": period.as_dict(previous=False),
        "age": {
            "age_days_at_arrival": batch.age_days,
            "days_since_arrival": days_since,
            "age_days_today": batch.age_days + days_since,
        },
        "totals": {
            "initial_quantity": batch.initial_quantity,
            "center_stock": center_stock,
            "in_households": in_households,
            "alive": center_stock + in_households,
            "distributed": distributed,
            "returned": returned,
            "deaths_total": deaths_total,
            "deaths_center": deaths_center,
            "deaths_households": deaths_households,
            "out_total": out_total,
            "out_center": out_center,
            "out_households": out_households,
            "households_now": sum(1 for entry in households.values() if entry["count_now"] > 0),
            "mortality_pct": round_half_up(metrics.percent(deaths_total, batch.initial_quantity), 1),
        },
        "households": [
            {
                "household": {"id": entry["id"], "code": f"XN-{entry['id']:04d}", "name": entry["name"]},
                "is_active": entry["is_active"],
                "count_now": entry["count_now"],
                "distributed": entry["distributed"],
                "returned": entry["returned"],
                "deaths": entry["deaths"],
                "out": entry["out"],
                "mortality_pct": round_half_up(metrics.percent(entry["deaths"], entry["distributed"]), 1),
            }
            for entry in household_rows
        ],
        "series": series,
    }


# --- §1.7 product stats -----------------------------------------------------------------------


def _active_from(created_at, first_tx) -> date | None:
    """Local day the product appeared: ``min(created_at day, first transaction day)``."""
    days = [timezone.localdate(value) for value in (created_at, first_tx) if value is not None]
    return min(days) if days else None


def _cover(
    balance: Decimal, out_cents: int, period: Period, active_from: date | None, today: date
) -> tuple[Decimal | None, Decimal | None, date | None]:
    days = metrics.effective_days(period.date_from, period.date_to, active_from)
    daily = metrics.avg_daily_out(metrics.cents_to_units(out_cents), days)
    cover = metrics.days_of_cover(balance, daily)
    return daily, cover, metrics.stockout_date(today, cover)


def _iso(value: date | None) -> str | None:
    return value.isoformat() if value is not None else None


def _stock_day_rows(qs, period: Period) -> list[tuple]:
    """``(product_id, type, local day, quantity cents, count)`` grouped by product, type and local day."""
    return list(
        qs.order_by()
        .annotate(day=queries.local_day(date_from=period.date_from, date_to=period.date_to))
        .values("product_id", "type", "day")
        .annotate(quantity=queries.int_sum(queries.cents("quantity")), count=Count("id"))
        .values_list("product_id", "type", "day", "quantity", "count")
    )


def product_stats(product: Product, product_data: dict, params: Mapping[str, Any], today: date | None = None):
    today = _today(today)
    period = resolve_period(params, today)
    days = period.days
    in_range = queries.stock_range(period.date_from, period.date_to)
    opening = (
        StockTransaction.objects.filter(product_id=product.pk, created_at__lt=in_range["created_at__gte"])
        .order_by("-id")
        .values_list("balance_after", flat=True)
        .first()
    )
    opening_cents = int((opening or Decimal(0)) * 100)
    first_tx = (
        StockTransaction.objects.filter(product_id=product.pk).aggregate(first=Min("created_at"))["first"]
    )
    day_rows = _stock_day_rows(StockTransaction.objects.filter(product_id=product.pk, **in_range), period)
    consumer_rows = list(
        StockTransaction.objects.filter(product_id=product.pk, type=StockTransactionType.OUT, **in_range)
        .exclude(returned_task_out())
        .order_by()
        .values("household_id", "household__name")
        .annotate(quantity=queries.int_sum(queries.cents("quantity")), count=Count("id"))
        .values_list("household_id", "household__name", "quantity", "count")
    )

    day_in = [0] * days
    day_out = [0] * days
    day_adjust = [0] * days  # V3 §5.6: signed net inventory adjustment of the day
    in_cents = out_cents = in_count = out_count = adjust_in_cents = adjust_out_cents = 0
    for _product_id, tx_type, day, quantity, count in day_rows:
        j = (day - period.date_from).days
        if not 0 <= j < days:
            continue
        if tx_type == StockTransactionType.IN:
            day_in[j] += quantity
            in_cents += quantity
            in_count += count
        elif tx_type == StockTransactionType.OUT:
            day_out[j] += quantity
            out_cents += quantity
            out_count += count
        elif tx_type == StockTransactionType.ADJUST_IN:
            day_adjust[j] += quantity
            adjust_in_cents += quantity
        elif tx_type == StockTransactionType.ADJUST_OUT:
            day_adjust[j] -= quantity
            adjust_out_cents += quantity

    series = []
    balance = opening_cents
    for j, day in enumerate(period.dates()):
        balance += day_in[j] - day_out[j] + day_adjust[j]
        series.append(
            {
                "date": day.isoformat(),
                "in_qty": _kg(day_in[j]),
                "out_qty": _kg(day_out[j]),
                "adjust_qty": _kg(day_adjust[j]),
                "balance": _kg(balance),
            }
        )

    daily, cover, stockout = _cover(
        product.balance, out_cents, period, _active_from(product.created_at, first_tx), today
    )
    consumers = []
    unassigned = 0
    for household_id, name, quantity, count in consumer_rows:
        if household_id is None:
            unassigned += quantity
        else:
            consumers.append((household_id, name, quantity, count))
    consumers.sort(key=lambda row: (-row[2], row[0]))
    return {
        "product": product_data,
        "period": period.as_dict(previous=False),
        "generated_at": queries.generated_at(),
        "totals": {
            "opening_balance": _kg(opening_cents),
            "in_qty": _kg(in_cents),
            "in_count": in_count,
            "out_qty": _kg(out_cents),
            "out_count": out_count,
            "net": _kg(in_cents - out_cents),
            "adjust_in_qty": _kg(adjust_in_cents),
            "adjust_out_qty": _kg(adjust_out_cents),
            "closing_balance": _kg(opening_cents + in_cents - out_cents + adjust_in_cents - adjust_out_cents),
        },
        "avg_daily_out": round_half_up(daily, 3),
        "days_of_cover": round_half_up(cover, 1),
        "stockout_date": _iso(stockout),
        "series": series,
        "top_consumers": [
            {
                "household": {"id": household_id, "code": f"XN-{household_id:04d}", "name": name},
                "quantity": _kg(quantity),
                "share_pct": round_half_up(metrics.share_pct(quantity, out_cents), 1),
                "transactions": count,
            }
            for household_id, name, quantity, count in consumers[:CONSUMERS_LIMIT]
        ],
        "out_unassigned_qty": _kg(unassigned),
    }


# --- §1.8 warehouse stats ---------------------------------------------------------------------


def _earliest(items: list[dict[str, Any]]) -> date | None:
    """Group ``active_from`` = the earliest product of the group (``None`` if unknown for all)."""
    days = [item["_active_from"] for item in items if item["_active_from"] is not None]
    return min(days) if days else None


def _is_feed_kg(product: Mapping[str, Any]) -> bool:
    return product["category"] == ProductCategory.FEED and product["unit"] == Unit.KG


def warehouse_stats(params: Mapping[str, Any], today: date | None = None) -> dict[str, Any]:
    today = _today(today)
    period = resolve_period(params, today)
    days = period.days
    in_range = queries.stock_range(period.date_from, period.date_to)
    products = list(
        Product.objects.order_by("name", "id")
        .annotate(first_tx=Min("transactions__created_at"))
        .values("id", "name", "category", "unit", "balance", "min_stock", "is_active", "created_at", "first_tx")
    )
    day_rows = _stock_day_rows(StockTransaction.objects.filter(**in_range), period)
    consumer_rows = list(
        StockTransaction.objects.filter(
            type=StockTransactionType.OUT,
            household__isnull=False,
            product__category=ProductCategory.FEED,
            product__unit=Unit.KG,
            **in_range,
        )
        .exclude(returned_task_out())
        .order_by()
        .values("household_id", "household__name")
        .annotate(quantity=queries.int_sum(queries.cents("quantity")), count=Count("id"))
        .values_list("household_id", "household__name", "quantity", "count")
    )

    by_product: dict[int, dict[str, int]] = defaultdict(lambda: {"in": 0, "out": 0, "adj_in": 0, "adj_out": 0, "tx": 0})
    feed_ids = {product["id"] for product in products if _is_feed_kg(product)}
    series_rows = [{"in_count": 0, "out_count": 0, "feed_in": 0, "feed_out": 0} for _ in range(days)]
    for product_id, tx_type, day, quantity, count in day_rows:
        j = (day - period.date_from).days
        if not 0 <= j < days:
            continue
        sums = by_product[product_id]
        sums["tx"] += count
        if tx_type == StockTransactionType.ADJUST_IN:  # V3 §5.6: adjustments are not IN/OUT
            sums["adj_in"] += quantity
            continue
        if tx_type == StockTransactionType.ADJUST_OUT:
            sums["adj_out"] += quantity
            continue
        if tx_type not in (StockTransactionType.IN, StockTransactionType.OUT):
            continue
        is_in = tx_type == StockTransactionType.IN
        sums["in" if is_in else "out"] += quantity
        series_rows[j]["in_count" if is_in else "out_count"] += count
        if product_id in feed_ids:
            series_rows[j]["feed_in" if is_in else "feed_out"] += quantity

    items = []
    for product in products:
        sums = by_product.get(product["id"])
        if not product["is_active"] and not sums:
            continue
        sums = sums or {"in": 0, "out": 0, "adj_in": 0, "adj_out": 0, "tx": 0}
        active_from = _active_from(product["created_at"], product["first_tx"])
        daily, cover, stockout = _cover(product["balance"], sums["out"], period, active_from, today)
        items.append(
            {
                "id": product["id"],
                "name": product["name"],
                "category": product["category"],
                "category_display": CATEGORY_LABELS[product["category"]],
                "unit": product["unit"],
                "balance": _amount(product["balance"]),
                "min_stock": _amount(product["min_stock"]),
                "is_low": product["balance"] <= product["min_stock"],
                "is_active": product["is_active"],
                "in_qty": _kg(sums["in"]),
                "out_qty": _kg(sums["out"]),
                "adjust_in_qty": _kg(sums["adj_in"]),
                "adjust_out_qty": _kg(sums["adj_out"]),
                "avg_daily_out": round_half_up(daily, 3),
                "days_of_cover": round_half_up(cover, 1),
                "stockout_date": _iso(stockout),
                "_in": sums["in"],
                "_out": sums["out"],
                "_adj_in": sums["adj_in"],
                "_adj_out": sums["adj_out"],
                "_balance": product["balance"],
                "_cover": cover,
                "_active_from": active_from,
            }
        )

    groups: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for item in items:
        groups[(item["category"], item["unit"])].append(item)
    by_category = []
    for (category, unit) in sorted(groups, key=lambda key: (CATEGORY_ORDER[key[0]], UNIT_ORDER[key[1]])):
        members = groups[(category, unit)]
        balance = sum((item["_balance"] for item in members), Decimal(0))
        out_cents = sum(item["_out"] for item in members)
        daily, cover, _ = _cover(balance, out_cents, period, _earliest(members), today)
        by_category.append(
            {
                "category": category,
                "category_display": CATEGORY_LABELS[category],
                "unit": unit,
                "products": len(members),
                "balance": _amount(balance),
                "in_qty": _kg(sum(item["_in"] for item in members)),
                "out_qty": _kg(out_cents),
                "adjust_in_qty": _kg(sum(item["_adj_in"] for item in members)),
                "adjust_out_qty": _kg(sum(item["_adj_out"] for item in members)),
                "avg_daily_out": round_half_up(daily, 3),
                "days_of_cover": round_half_up(cover, 1),
            }
        )

    feed_items = [item for item in items if item["id"] in feed_ids]
    feed_balance = sum(
        (product["balance"] for product in products if product["id"] in feed_ids and product["is_active"]), Decimal(0)
    )
    feed_in = sum(item["_in"] for item in feed_items)
    feed_out = sum(item["_out"] for item in feed_items)
    feed_daily, feed_cover, feed_stockout = _cover(feed_balance, feed_out, period, _earliest(feed_items), today)

    low = [item for item in items if item["is_active"] and item["is_low"]]
    low.sort(key=lambda item: (item["_cover"] is None, item["_cover"] or 0, item["name"]))

    consumers = sorted(consumer_rows, key=lambda row: (-row[2], row[0]))[:CONSUMERS_LIMIT]
    active_count = sum(1 for product in products if product["is_active"])
    low_count = sum(1 for product in products if product["is_active"] and product["balance"] <= product["min_stock"])
    return {
        "period": period.as_dict(previous=False),
        "generated_at": queries.generated_at(),
        "products": {"total": len(products), "active": active_count, "low": low_count},
        "feed": {
            "balance_kg": _amount(feed_balance),
            "in_kg": _kg(feed_in),
            "out_kg": _kg(feed_out),
            "avg_daily_out_kg": round_half_up(feed_daily, 3),
            "days_of_cover": round_half_up(feed_cover, 1),
            "stockout_date": _iso(feed_stockout),
        },
        "series": [
            {
                "date": day.isoformat(),
                "in_count": series_rows[j]["in_count"],
                "out_count": series_rows[j]["out_count"],
                "feed_in_kg": _kg(series_rows[j]["feed_in"]),
                "feed_out_kg": _kg(series_rows[j]["feed_out"]),
            }
            for j, day in enumerate(period.dates())
        ],
        "by_category": by_category,
        "items": [{key: value for key, value in item.items() if not key.startswith("_")} for item in items],
        "low_stock": [
            {
                key: item[key]
                for key in ("id", "name", "unit", "balance", "min_stock", "avg_daily_out", "days_of_cover",
                            "stockout_date")
            }
            for item in low
        ],
        "top_consumers": [
            {
                "household": {"id": household_id, "code": f"XN-{household_id:04d}", "name": name},
                "feed_kg": _kg(quantity),
                "share_pct": round_half_up(metrics.share_pct(quantity, feed_out), 1),
                "transactions": count,
            }
            for household_id, name, quantity, count in consumers
        ],
    }


# --- §5.1 bot summary -------------------------------------------------------------------------


def bot_summary(user: User, today: date | None = None) -> dict[str, Any]:
    if not user.is_active:
        raise SummaryError(403, "Hisobingiz bloklangan. Administratorga murojaat qiling.")
    today = _today(today)
    role = role_of(user)
    base = {"role": role, "date": today.isoformat(), "generated_at": queries.generated_at()}
    if role == Role.OWNER:
        household = getattr(user, "household", None)
        if household is None:
            raise SummaryError(403, MSG_NO_HOUSEHOLD)
        return {**base, **_owner_summary(household, today)}
    if role == Role.LOGISTICS:  # V3 §10.3 (logistics team)
        from logistics.selectors import bot_today as logistics_today

        return {**base, "today": logistics_today(user)}
    if role == Role.VET:  # V3 §10.3 (vet team)
        from vet.selectors import bot_today

        return {**base, "today": bot_today(today)}
    return {**base, "today": _staff_today(today)}


def _owner_summary(household: Household, today: date) -> dict[str, Any]:
    period = Period(today - timedelta(days=SUMMARY_DAYS - 1), today)
    scope = _flock_scope(period, [queries.meta_of(household)], today, household_id=household.pk, previous=False)
    state = scope.states[household.pk]
    current = state.current
    last = SUMMARY_DAYS - 1
    submitted = last in scope.reported_days
    meat_rows = dict(
        DailyReport.objects.filter(household=household, date__gte=period.date_from, date__lte=today)
        .order_by()
        .values_list("date", "meat_kg")
    )
    meat_today = meat_rows.get(today)
    meat_week = sum((value for value in meat_rows.values() if value is not None), Decimal(0))
    return {
        "household": {"id": household.pk, "code": household.code, "name": household.name},
        "bird_count": state.all.at(scope.current[1]),
        "today": {
            "report_submitted": submitted,
            "eggs": scope.series.eggs[last] if submitted else None,
            "feed_kg": _kg(scope.series.feed_cents[last]) if submitted else None,
            "meat_kg": float(meat_today) if submitted and meat_today is not None else None,
            "deaths": scope.series.deaths[last],
        },
        "last_7d": {
            **period.as_dict(previous=False),
            "reports": current.reports,
            "reported": current.reported_expected,
            "expected": current.expected,
            "eggs": current.eggs,
            "feed_kg": _kg(current.feed_cents),
            "meat_kg": float(meat_week),
            "deaths": current.deaths,
            "laying_rate_pct": round_half_up(metrics.laying_rate_pct(current.rl_eggs, current.rl_layer_days), 1),
        },
    }


def _staff_today(today: date) -> dict[str, Any]:
    """Today's company numbers, identical to ``/api/stats/overview`` ``series[today]`` (V2 §5.1 as amended).

    ``households_expected`` = households due today (active, created, ``B_h(today) > 0``) = ``expected_reports``;
    ``households_reported`` = those with a report today = ``reported_expected``; ``deaths`` = household
    DEATH movements of today (= ``series.deaths``), center deaths separately in ``deaths_center``.
    """
    households = list(Household.objects.order_by().values_list("id", "is_active", "created_at"))
    reports = list(
        DailyReport.objects.filter(date=today)
        .order_by()
        .values_list("household_id", "eggs", queries.cents("feed_kg"), "meat_kg")
    )
    flock_rows = list(
        PoultryMovement.objects.filter(date__lte=today)
        .order_by()
        .values("household_id", "batch__poultry_type")
        .annotate(
            household_net=queries.int_sum(queries.HOUSEHOLD_DELTA),
            center_net=queries.int_sum(queries.CENTER_DELTA),
            deaths_today=queries.int_sum("quantity", Q(type=MovementType.DEATH, date=today)),
        )
        .values_list("household_id", "batch__poultry_type", "household_net", "center_net", "deaths_today")
    )
    initial = queries.batches_initial_through(today)
    low_stock_count = Product.objects.filter(is_active=True, balance__lte=F("min_stock")).count()

    layers: dict[int, int] = defaultdict(int)
    birds: dict[int, int] = defaultdict(int)
    in_households = center_net = deaths = deaths_center = 0
    for household_id, poultry_type, household_net, row_center, deaths_today in flock_rows:
        center_net += row_center
        if household_id is None:
            deaths_center += deaths_today
            continue
        deaths += deaths_today
        in_households += household_net
        birds[household_id] += household_net
        if poultry_type == PoultryType.LAYER:
            layers[household_id] += household_net
    due = {
        household_id
        for household_id, is_active, created_at in households
        if is_active and timezone.localdate(created_at) <= today and birds.get(household_id, 0) > 0
    }
    eggs = feed_cents = reported_due = layer_eggs = layer_birds = 0
    meat = Decimal(0)
    for household_id, report_eggs, report_feed, report_meat in reports:
        meat += report_meat or Decimal(0)
        eggs += report_eggs
        feed_cents += report_feed
        reported_due += 1 if household_id in due else 0
        layer_count = layers.get(household_id, 0)
        if layer_count > 0:
            layer_eggs += report_eggs
            layer_birds += layer_count
    in_center = initial + center_net
    return {
        "households_active": sum(1 for _id, is_active, _created in households if is_active),
        "households_expected": len(due),
        "households_reported": reported_due,
        "report_discipline_pct": round_half_up(metrics.percent(reported_due, len(due)), 1),
        "eggs": eggs,
        "feed_kg": _kg(feed_cents),
        "meat_kg": float(meat),
        "laying_rate_pct": round_half_up(metrics.laying_rate_pct(layer_eggs, layer_birds), 1),
        "deaths": deaths,
        "deaths_center": deaths_center,
        "birds_in_households": in_households,
        "birds_in_center": in_center,
        "birds_total": in_households + in_center,
        "low_stock_count": low_stock_count,
    }

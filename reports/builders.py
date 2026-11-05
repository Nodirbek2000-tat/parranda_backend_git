"""Data builders of the 10 reports (API_CONTRACT_V3 §11.2).

Numbers reuse the V2 definitions (single source of truth: :mod:`stats.flock`, :mod:`stats.metrics`,
:mod:`stats.queries`): bird counts ``B_h(d)`` come from :class:`stats.flock.FlockWindow`, period totals from
:class:`stats.flock.ScopeStats` and every ratio from :mod:`stats.metrics`. Each builder runs a fixed number of
aggregate queries (≤ 12, independent of the number of rows).

Data of the other V3 modules (orders, logistics, vet) is read through
:func:`warehouse.readiness.optional_model`, so a report still works (with zeros) while such a module is not
installed or not migrated yet.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import date, timedelta
from decimal import Decimal
from typing import Any

from django.db.models import Count, F, Max, Q, Sum
from django.utils import timezone

from common.periods import day_range
from farm.models import DailyReport, Household, MovementType, PoultryBatch, PoultryMovement, PoultryType
from farm.selectors import batches_with_counts
from logistics.selectors import delivery_kpis, is_on_time
from stats import metrics, queries
from stats.flock import FlockWindow, HouseholdMeta, HouseholdState, ScopeStats
from stats.metrics import Totals
from stats.periods import Period
from warehouse import lots as lot_reads
from warehouse.models import Product, ProductCategory, StockTransaction, StockTransactionType, Unit, returned_task_out
from warehouse.readiness import models_ready, optional_model

from .core import BOOL, DATE, DATETIME, DECIMAL, INT, PERCENT, TEXT, ReportData, col
from .params import POULTRY_LABELS, ReportParams

MOVEMENT_LABELS = dict(MovementType.choices)
CATEGORY_LABELS = dict(ProductCategory.choices)
CENTER = "Markaz"


def _code(household_id: int) -> str:
    return f"XN-{household_id:04d}"


def _kg(cents: int | Decimal) -> Decimal:
    return metrics.cents_to_units(cents)


def _cents(value: Decimal | None) -> int:
    return int((value or Decimal(0)) * 100)


def _households(p: ReportParams):
    """Households of the region / district / household filters (OWNER: forced to the own household)."""
    qs = Household.objects.all()
    if p.household is not None:
        qs = qs.filter(pk=p.household.pk)
    if p.region:
        qs = qs.filter(region=p.region)
    if p.district:
        qs = qs.filter(district=p.district)
    return qs


def _filtered(p: ReportParams) -> bool:
    return p.household is not None or bool(p.region) or bool(p.district)


class Scope:
    """Flock + reports of the filtered households over the report period (4 queries)."""

    def __init__(self, p: ReportParams, *, metas: list[HouseholdMeta] | None = None, report_rows: list | None = None):
        period = p.period
        self.p = p
        self.period = period
        household_id = p.household.pk if p.household is not None else None
        if metas is None:
            metas = queries.household_meta(_households(p))
        self.window = FlockWindow(
            period.date_from,
            period.date_to,
            queries.movement_opening(period.date_from, household_id),
            queries.movement_daily(period.date_from, period.date_to, household_id),
        )
        if p.poultry_type:
            # §11.1: the household set = households with ≥ 1 movement of a batch of that type.
            metas = [meta for meta in metas if p.poultry_type in self.window.types_of(meta.id)]
        self.metas = metas
        if report_rows is None:
            report_rows = queries.report_rows(period.date_from, period.date_to, household_id)
        self.report_rows = report_rows
        self.stats = self.scope(0, period.days - 1, report_rows)

    def scope(self, first: int, last: int, report_rows) -> ScopeStats:
        return ScopeStats(self.window, self.metas, report_rows, current=(first, last), today=self.p.today)

    def birds(self, state: HouseholdState, totals: Totals) -> Totals:
        """Bird / death numbers restricted to the poultry type filter (eggs and feed stay per household)."""
        if not self.p.poultry_type:
            return totals
        return state.type_totals.get(self.p.poultry_type) or Totals()


def _pct(value) -> Decimal | None:
    return value


# --- 1. daily_production ---------------------------------------------------------------------------


def daily_production(p: ReportParams) -> ReportData:
    period = p.period
    reports_qs = DailyReport.objects.filter(date__gte=period.date_from, date__lte=period.date_to)
    if _filtered(p):
        reports_qs = reports_qs.filter(household__in=_households(p).values("pk"))
    full = list(
        reports_qs.order_by("date", "household_id").values_list(
            "household_id", "date", "eggs", "feed_kg", "meat_kg", "deaths", "avg_weight_g", "note"
        )
    )
    tuples = [(hid, (day - period.date_from).days, eggs, _cents(feed)) for hid, day, eggs, feed, *_ in full]
    scope = Scope(p, report_rows=tuples)
    states = scope.stats.states
    rows: list[dict[str, Any]] = []
    meat_cents = 0
    deaths = 0
    for hid, day, eggs, feed, meat, report_deaths, avg_weight, note in full:
        state = states.get(hid)
        if state is None:
            continue
        index = (day - scope.window.start).days
        step = state.by_type.get(p.poultry_type) if p.poultry_type else state.all
        birds = step.at(index) if step is not None else 0
        layers = state.layer.at(index) if state.layer is not None else 0
        meat_cents += _cents(meat)
        deaths += report_deaths
        rows.append(
            {
                "date": day,
                "household_code": _code(hid),
                "household": state.meta.name,
                "district": state.meta.district,
                "birds": birds,
                "eggs": eggs,
                "laying_rate_pct": metrics.laying_rate_pct(eggs, layers) if layers > 0 else None,
                "feed_kg": feed,
                "meat_kg": meat,
                "deaths": report_deaths,
                "avg_weight_g": avg_weight,
                "note": note,
            }
        )
    totals_src = scope.stats.total_current()
    values = metrics.kpi_values(totals_src, period.days)
    totals = {
        "eggs": totals_src.eggs,
        "laying_rate_pct": values["laying_rate_pct"],
        "feed_kg": _kg(totals_src.feed_cents),
        "meat_kg": _kg(meat_cents),
        "deaths": deaths,
    }
    households = len({row["household_code"] for row in rows})
    summary = [
        ("Xonadonlar", str(households)),
        ("Hisobotlar", str(len(rows))),
        ("Tuxum", _num(totals_src.eggs)),
        ("Tuxumdorlik", _percent(values["laying_rate_pct"])),
        ("Yem, kg", _dec(_kg(totals_src.feed_cents))),
        ("Hisobot intizomi", _percent(values["report_discipline_pct"])),
    ]
    columns = [
        col("date", "Sana", DATE),
        col("household_code", "Xonadon kodi"),
        col("household", "Xonadon"),
        col("district", "Tuman"),
        col("birds", "Parranda", INT),
        col("eggs", "Tuxum", INT),
        col("laying_rate_pct", "Tuxumdorlik %", PERCENT, 1),
        col("feed_kg", "Yem kg", DECIMAL, 2),
        col("meat_kg", "Go‘sht kg", DECIMAL, 2),
        col("deaths", "Nobud", INT),
        col("avg_weight_g", "O‘rtacha vazn g", DECIMAL, 1),
        col("note", "Izoh"),
    ]
    return ReportData(columns, rows, totals, summary)


# --- 2. monthly_production -------------------------------------------------------------------------


def _months(period: Period) -> list[tuple[date, date]]:
    result = []
    day = period.date_from
    while day <= period.date_to:
        nxt = (day.replace(day=28) + timedelta(days=4)).replace(day=1)
        result.append((day, min(nxt - timedelta(days=1), period.date_to)))
        day = nxt
    return result


def monthly_production(p: ReportParams) -> ReportData:
    period = p.period
    reports_qs = DailyReport.objects.filter(date__gte=period.date_from, date__lte=period.date_to)
    if _filtered(p):
        reports_qs = reports_qs.filter(household__in=_households(p).values("pk"))
    full = list(reports_qs.order_by().values_list("household_id", "date", "eggs", "feed_kg", "meat_kg"))
    tuples = [(hid, (day - period.date_from).days, eggs, _cents(feed)) for hid, day, eggs, feed, _meat in full]
    scope = Scope(p, report_rows=tuples)
    member = set(scope.stats.states)
    by_month: dict[tuple[int, int], list] = defaultdict(list)
    meat: dict[tuple[int, int], int] = defaultdict(int)
    reporters: dict[tuple[int, int], set[int]] = defaultdict(set)
    for (hid, day, _eggs, _feed, meat_kg), item in zip(full, tuples):
        if hid not in member:
            continue
        key = (day.year, day.month)
        by_month[key].append(item)
        meat[key] += _cents(meat_kg)
        reporters[key].add(hid)
    rows = []
    for start, end in _months(period):
        key = (start.year, start.month)
        first, last = (start - period.date_from).days, (end - period.date_from).days
        month_scope = scope.scope(first, last, by_month.get(key, []))
        totals = month_scope.total_current()
        days = last - first + 1
        bird_days, deaths = _bird_numbers(scope, month_scope, totals)
        rows.append(
            {
                "month": f"{start.month:02d}.{start.year}",
                "households_reported": len(reporters.get(key, ())),
                "reports": totals.reports,
                "eggs": totals.eggs,
                "laying_rate_pct": metrics.laying_rate_pct(totals.rl_eggs, totals.rl_layer_days),
                "feed_kg": _kg(totals.feed_cents),
                "feed_per_egg_g": metrics.feed_per_egg_g(totals.rl_feed_cents, totals.rl_eggs),
                "meat_kg": _kg(meat.get(key, 0)),
                "deaths": deaths,
                "mortality_pct": metrics.mortality_pct(deaths, bird_days, days),
            }
        )
    all_totals = scope.stats.total_current()
    bird_days, deaths = _bird_numbers(scope, scope.stats, all_totals)
    totals = {
        "households_reported": len(set().union(*reporters.values())) if reporters else 0,
        "reports": all_totals.reports,
        "eggs": all_totals.eggs,
        "laying_rate_pct": metrics.laying_rate_pct(all_totals.rl_eggs, all_totals.rl_layer_days),
        "feed_kg": _kg(all_totals.feed_cents),
        "feed_per_egg_g": metrics.feed_per_egg_g(all_totals.rl_feed_cents, all_totals.rl_eggs),
        "meat_kg": _kg(sum(meat.values())),
        "deaths": deaths,
        "mortality_pct": metrics.mortality_pct(deaths, bird_days, period.days),
    }
    summary = [
        ("Oylar", str(len(rows))),
        ("Xonadonlar", str(len(scope.metas))),
        ("Tuxum", _num(all_totals.eggs)),
        ("Nobud", _num(deaths)),
    ]
    columns = [
        col("month", "Oy"),
        col("households_reported", "Hisobot bergan xonadonlar", INT),
        col("reports", "Hisobotlar", INT),
        col("eggs", "Tuxum", INT),
        col("laying_rate_pct", "Tuxumdorlik %", PERCENT, 1),
        col("feed_kg", "Yem kg", DECIMAL, 2),
        col("feed_per_egg_g", "Yem/tuxum g", DECIMAL, 1),
        col("meat_kg", "Go‘sht kg", DECIMAL, 2),
        col("deaths", "Nobud", INT),
        col("mortality_pct", "O‘lim %", PERCENT, 1),
    ]
    return ReportData(columns, rows, totals, summary)


def _bird_numbers(scope: Scope, stats: ScopeStats, totals: Totals) -> tuple[int, int]:
    """``(bird_days, deaths)`` of a scope, restricted to the poultry type filter when set."""
    if not scope.p.poultry_type:
        return totals.bird_days, totals.deaths
    _count, merged = stats.type_totals(scope.p.poultry_type)
    return merged.bird_days, merged.deaths


# --- 3. poultry_movements --------------------------------------------------------------------------


def poultry_movements(p: ReportParams) -> ReportData:
    period = p.period
    qs = PoultryMovement.objects.filter(date__gte=period.date_from, date__lte=period.date_to)
    if _filtered(p):
        qs = qs.filter(household__in=_households(p).values("pk"))
    if p.poultry_type:
        qs = qs.filter(batch__poultry_type=p.poultry_type)
    qs = qs.select_related("batch", "household", "created_by").order_by("date", "id")
    rows = []
    by_type: dict[str, int] = defaultdict(int)
    for movement in qs:
        by_type[movement.type] += movement.quantity
        household = movement.household
        rows.append(
            {
                "op_id": movement.op_id,
                "date": movement.date,
                "type": MOVEMENT_LABELS.get(movement.type, movement.type),
                "batch": movement.batch.code,
                "poultry_type": POULTRY_LABELS.get(movement.batch.poultry_type, movement.batch.poultry_type),
                "household": f"{household.code} {household.name}" if household is not None else CENTER,
                "quantity": movement.quantity,
                "note": movement.note,
                "created_by": movement.created_by.full_name if movement.created_by_id else "",
            }
        )
    summary = [("Harakatlar", str(len(rows)))]
    summary += [(MOVEMENT_LABELS[kind], _num(by_type[kind])) for kind in MovementType.values if by_type.get(kind)]
    columns = [
        col("op_id", "Op ID"),
        col("date", "Sana", DATE),
        col("type", "Turi"),
        col("batch", "Partiya"),
        col("poultry_type", "Parranda turi"),
        col("household", "Xonadon"),
        col("quantity", "Miqdor", INT),
        col("note", "Izoh"),
        col("created_by", "Kiritgan"),
    ]
    return ReportData(columns, rows, {}, summary)


# --- 4. households_activity ------------------------------------------------------------------------


def _metas_with_owner(p: ReportParams) -> tuple[list[HouseholdMeta], dict[int, dict[str, Any]]]:
    rows = _households(p).order_by("id").values(
        "id", "name", "region", "district", "is_active", "created_at", "phone",
        "owner__first_name", "owner__last_name",
    )
    metas, extra = [], {}
    for row in rows:
        metas.append(
            HouseholdMeta(
                id=row["id"], name=row["name"], region=row["region"], district=row["district"],
                is_active=row["is_active"], created_day=timezone.localdate(row["created_at"]),
            )
        )
        owner = f"{row['owner__first_name']} {row['owner__last_name']}".strip()
        extra[row["id"]] = {"owner": owner, "phone": row["phone"]}
    return metas, extra


def _range(period: Period, field_name: str) -> dict[str, Any]:
    start, end = day_range(period.date_from, period.date_to)
    return {f"{field_name}__gte": start, f"{field_name}__lt": end}


def _orders_by_household(period: Period, ids: list[int]) -> dict[int, int]:
    model = optional_model("orders", "Order", "household", "created_at")
    if model is None or not ids:
        return {}
    rows = (
        model.objects.filter(household_id__in=ids, **_range(period, "created_at"))
        .order_by()
        .values("household_id")
        .annotate(n=Count("id"))
        .values_list("household_id", "n")
    )
    return dict(rows)


def _deliveries_by_household(period: Period, ids: list[int]) -> dict[int, int]:
    model = optional_model("logistics", "LogisticsTask", "household", "delivered_at", "status")
    if model is None or not ids:
        return {}
    rows = (
        model.objects.filter(household_id__in=ids, status__in=("DELIVERED", "CONFIRMED"), **_range(period, "delivered_at"))
        .order_by()
        .values("household_id")
        .annotate(n=Count("id"))
        .values_list("household_id", "n")
    )
    return dict(rows)


def households_activity(p: ReportParams) -> ReportData:
    metas, extra = _metas_with_owner(p)
    scope = Scope(p, metas=metas)
    ids = [meta.id for meta in scope.metas]
    last_reports = dict(
        DailyReport.objects.filter(household_id__in=ids, date__lte=p.period.date_to)
        .order_by()
        .values("household_id")
        .annotate(last=Max("date"))
        .values_list("household_id", "last")
    )
    orders = _orders_by_household(p.period, ids)
    deliveries = _deliveries_by_household(p.period, ids)
    rows = []
    for meta in scope.metas:
        state = scope.stats.states[meta.id]
        totals = state.current
        rows.append(
            {
                "code": meta.code,
                "household": meta.name,
                "owner": extra[meta.id]["owner"],
                "phone": extra[meta.id]["phone"],
                "region": meta.region,
                "district": meta.district,
                "is_active": meta.is_active,
                "birds_end": totals.birds_end,
                "reports": totals.reports,
                "expected": totals.expected,
                "discipline_pct": metrics.discipline_pct(totals.reported_expected, totals.expected),
                "last_report": last_reports.get(meta.id),
                "orders": orders.get(meta.id, 0),
                "deliveries": deliveries.get(meta.id, 0),
            }
        )
    current = scope.stats.total_current()
    totals = {
        "birds_end": current.birds_end,
        "reports": current.reports,
        "expected": current.expected,
        "discipline_pct": metrics.discipline_pct(current.reported_expected, current.expected),
        "orders": sum(orders.values()),
        "deliveries": sum(deliveries.values()),
    }
    summary = [
        ("Xonadonlar", str(len(rows))),
        ("Faol xonadonlar", str(sum(1 for meta in scope.metas if meta.is_active))),
        ("Hisobot intizomi", _percent(totals["discipline_pct"])),
    ]
    columns = [
        col("code", "Kod"),
        col("household", "Xonadon"),
        col("owner", "Egasi"),
        col("phone", "Telefon"),
        col("region", "Viloyat"),
        col("district", "Tuman"),
        col("is_active", "Faol", BOOL),
        col("birds_end", "Parranda (oxirida)", INT),
        col("reports", "Hisobotlar", INT),
        col("expected", "Kutilgan", INT),
        col("discipline_pct", "Intizom %", PERCENT, 1),
        col("last_report", "Oxirgi hisobot", DATE),
        col("orders", "Buyurtmalar", INT),
        col("deliveries", "Yetkazib berishlar", INT),
    ]
    return ReportData(columns, rows, totals, summary)


# --- 5. mortality ----------------------------------------------------------------------------------


def _open_diseases(ids: list[int]) -> dict[int, int]:
    model = optional_model("vet", "DiseaseRecord", "household", "status")
    if model is None or not ids:
        return {}
    return dict(
        model.objects.filter(household_id__in=ids)
        .exclude(status="RESOLVED")
        .order_by()
        .values("household_id")
        .annotate(n=Count("id"))
        .values_list("household_id", "n")
    )


def mortality(p: ReportParams) -> ReportData:
    scope = Scope(p)
    days = p.period.days
    ids = [meta.id for meta in scope.metas]
    diseases = _open_diseases(ids)
    rows = []
    for meta in scope.metas:
        state = scope.stats.states[meta.id]
        birds = scope.birds(state, state.current)
        if not birds.bird_days and not birds.deaths and not diseases.get(meta.id):
            continue
        rows.append(
            {
                "code": meta.code,
                "household": meta.name,
                "district": meta.district,
                "birds_avg": metrics.birds_avg(birds.bird_days, days),
                "deaths": birds.deaths,
                "mortality_pct": metrics.mortality_pct(birds.deaths, birds.bird_days, days),
                "open_diseases": diseases.get(meta.id, 0),
            }
        )
    bird_days, deaths = _bird_numbers(scope, scope.stats, scope.stats.total_current())
    totals = {
        "birds_avg": metrics.birds_avg(bird_days, days),
        "deaths": deaths,
        "mortality_pct": metrics.mortality_pct(deaths, bird_days, days),
        "open_diseases": sum(diseases.values()),
    }
    summary = [("Xonadonlar", str(len(rows)))]
    types = [p.poultry_type] if p.poultry_type else list(PoultryType.values)
    for poultry_type in types:
        _count, merged = scope.stats.type_totals(poultry_type)
        if not merged.bird_days and not merged.deaths:
            continue
        pct = metrics.mortality_pct(merged.deaths, merged.bird_days, days)
        summary.append(
            (
                POULTRY_LABELS[poultry_type],
                f"o‘rtacha {_dec(metrics.birds_avg(merged.bird_days, days), 1)}, nobud {_num(merged.deaths)}, "
                f"o‘lim {_percent(pct)}",
            )
        )
    columns = [
        col("code", "Kod"),
        col("household", "Xonadon"),
        col("district", "Tuman"),
        col("birds_avg", "O‘rtacha parranda", DECIMAL, 1),
        col("deaths", "Nobud", INT),
        col("mortality_pct", "O‘lim %", PERCENT, 1),
        col("open_diseases", "Ochiq kasalliklar", INT),
    ]
    return ReportData(columns, rows, totals, summary)


# --- 6. feed_consumption ---------------------------------------------------------------------------


def _issued_feed(p: ReportParams, ids: list[int]) -> dict[int, int]:
    if not ids:
        return {}
    rows = (
        StockTransaction.objects.filter(
            type=StockTransactionType.OUT,
            household_id__in=ids,
            product__category=ProductCategory.FEED,
            product__unit=Unit.KG,
            **queries.stock_range(p.period.date_from, p.period.date_to),
        )
        .exclude(returned_task_out())
        .order_by()
        .values("household_id")
        .annotate(cents=queries.int_sum(queries.cents("quantity")))
        .values_list("household_id", "cents")
    )
    return dict(rows)


def feed_consumption(p: ReportParams) -> ReportData:
    scope = Scope(p)
    days = p.period.days
    ids = [meta.id for meta in scope.metas]
    issued = _issued_feed(p, ids)
    rows = []
    for meta in scope.metas:
        totals = scope.stats.states[meta.id].current
        if not totals.feed_cents and not issued.get(meta.id) and not totals.bird_days:
            continue
        rows.append(
            {
                "code": meta.code,
                "household": meta.name,
                "district": meta.district,
                "feed_reported_kg": _kg(totals.feed_cents),
                "feed_issued_kg": _kg(issued.get(meta.id, 0)),
                "birds_avg": metrics.birds_avg(totals.bird_days, days),
                "feed_per_bird_day_kg": metrics.feed_per_bird_day_kg(totals.rp_feed_cents, totals.rp_bird_days),
                "feed_per_egg_g": metrics.feed_per_egg_g(totals.rl_feed_cents, totals.rl_eggs),
            }
        )
    current = scope.stats.total_current()
    totals = {
        "feed_reported_kg": _kg(current.feed_cents),
        "feed_issued_kg": _kg(sum(issued.values())),
        "birds_avg": metrics.birds_avg(current.bird_days, days),
        "feed_per_bird_day_kg": metrics.feed_per_bird_day_kg(current.rp_feed_cents, current.rp_bird_days),
        "feed_per_egg_g": metrics.feed_per_egg_g(current.rl_feed_cents, current.rl_eggs),
    }
    summary = [
        ("Xonadonlar", str(len(rows))),
        ("Hisobotdagi yem, kg", _dec(totals["feed_reported_kg"])),
        ("Ombordan berilgan yem, kg", _dec(totals["feed_issued_kg"])),
    ]
    columns = [
        col("code", "Kod"),
        col("household", "Xonadon"),
        col("district", "Tuman"),
        col("feed_reported_kg", "Hisobotdagi yem kg", DECIMAL, 2),
        col("feed_issued_kg", "Ombordan berilgan yem kg", DECIMAL, 2),
        col("birds_avg", "O‘rtacha parranda", DECIMAL, 1),
        col("feed_per_bird_day_kg", "Yem/parranda/kun kg", DECIMAL, 3),
        col("feed_per_egg_g", "Yem/tuxum g", DECIMAL, 1),
    ]
    return ReportData(columns, rows, totals, summary)


# --- 7. stock_balance ------------------------------------------------------------------------------


def _signed_cents():
    from django.db.models import Case, When, Value
    from django.db.models import BigIntegerField

    amount = queries.cents("quantity")
    return Case(
        When(type__in=[StockTransactionType.OUT, StockTransactionType.ADJUST_OUT], then=-amount),
        default=amount,
        output_field=BigIntegerField(),
    )


def stock_balance(p: ReportParams) -> ReportData:
    """Movements of the period per product; ``Yakuniy`` = the balance at the end of ``date_to``."""
    period = p.period
    start, end = day_range(period.date_from, period.date_to)
    products = Product.objects.order_by("category", "name", "id")
    if models_ready(lot_reads.StockLot):
        products = products.annotate(nearest_expiry=lot_reads.nearest_expiry_subquery())
    products = list(products)
    opening = dict(
        StockTransaction.objects.filter(created_at__lt=start)
        .order_by()
        .values("product_id")
        .annotate(cents=queries.int_sum(_signed_cents()))
        .values_list("product_id", "cents")
    )
    moves: dict[int, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    for product_id, tx_type, cents in (
        StockTransaction.objects.filter(created_at__gte=start, created_at__lt=end)
        .order_by()
        .values("product_id", "type")
        .annotate(cents=queries.int_sum(queries.cents("quantity")))
        .values_list("product_id", "type", "cents")
    ):
        moves[product_id][tx_type] += cents
    rows = []
    low = 0
    for product in products:
        sums = moves.get(product.pk, {})
        opening_cents = opening.get(product.pk, 0)
        if not product.is_active and not sums and not product.balance:
            continue
        adjust = sums.get(StockTransactionType.ADJUST_IN, 0) - sums.get(StockTransactionType.ADJUST_OUT, 0)
        closing = opening_cents + sums.get(StockTransactionType.IN, 0) - sums.get(StockTransactionType.OUT, 0) + adjust
        is_low = product.balance <= product.min_stock
        low += 1 if is_low and product.is_active else 0
        rows.append(
            {
                "product": product.name,
                "category": CATEGORY_LABELS.get(product.category, product.category),
                "unit": product.unit,
                "opening": _kg(opening_cents),
                "in_qty": _kg(sums.get(StockTransactionType.IN, 0)),
                "out_qty": _kg(sums.get(StockTransactionType.OUT, 0)),
                "adjust_qty": _kg(adjust),
                "closing": _kg(closing),
                "balance": product.balance,
                "reserved": product.reserved,
                "available": product.balance - product.reserved,
                "min_stock": product.min_stock,
                "is_low": is_low,
                "nearest_expiry": getattr(product, "nearest_expiry", None),
            }
        )
    # TZ §7 "Parranda" category: birds are kept as batches (PoultryBatch), not as warehouse products
    # (deviation V3 §20.7). The center's current stock per batch is listed in the same report.
    center_birds = 0
    for batch in batches_with_counts(PoultryBatch.objects.order_by("arrived_at", "id")):
        if batch.center_stock <= 0:
            continue
        center_birds += batch.center_stock
        rows.append(
            {
                "product": f"{batch.code} · {batch.breed}",
                "category": "Parranda (markaz)",
                "unit": "dona",
                "opening": None,
                "in_qty": None,
                "out_qty": None,
                "adjust_qty": None,
                "closing": None,
                "balance": batch.center_stock,
                "reserved": None,
                "available": batch.center_stock,
                "min_stock": None,
                "is_low": None,
                "nearest_expiry": None,
            }
        )
    summary = [
        ("Mahsulotlar", str(len(rows) - sum(1 for row in rows if row["category"] == "Parranda (markaz)"))),
        ("Minimal zaxiradan kam", str(low)),
        ("Markazdagi parranda, dona", str(center_birds)),
        ("Holat sanasi", period.date_to.strftime("%d.%m.%Y")),
    ]
    columns = [
        col("product", "Mahsulot"),
        col("category", "Toifa"),
        col("unit", "Birlik"),
        col("opening", "Boshlang‘ich", DECIMAL, 2),
        col("in_qty", "Kirim", DECIMAL, 2),
        col("out_qty", "Chiqim", DECIMAL, 2),
        col("adjust_qty", "Inventarizatsiya ±", DECIMAL, 2),
        col("closing", "Yakuniy", DECIMAL, 2),
        col("balance", "Joriy qoldiq", DECIMAL, 2),
        col("reserved", "Zaxirada", DECIMAL, 2),
        col("available", "Bo‘sh", DECIMAL, 2),
        col("min_stock", "Minimal", DECIMAL, 2),
        col("is_low", "Kam", BOOL),
        col("nearest_expiry", "Eng yaqin muddat", DATE),
    ]
    return ReportData(columns, rows, {}, summary)


# --- 8. logistics_efficiency -----------------------------------------------------------------------


def _hours(start, end) -> Decimal | None:
    if start is None or end is None:
        return None
    return Decimal((end - start).total_seconds()) / Decimal(3600)


def _hours_avg(values: list[float]) -> Decimal:
    return sum((Decimal(str(value)) for value in values), Decimal(0)) / Decimal(len(values))


def logistics_efficiency(p: ReportParams) -> ReportData:
    columns = [
        col("code", "Kod"),
        col("household", "Xonadon"),
        col("status", "Holat"),
        col("created_at", "Yaratilgan", DATETIME),
        col("planned_date", "Reja sanasi", DATE),
        col("started_at", "Yo‘lga chiqdi", DATETIME),
        col("delivered_at", "Yetkazildi", DATETIME),
        col("confirmed_at", "Tasdiqlandi", DATETIME),
        col("lead_hours", "Muddat (soat)", DECIMAL, 1),
        col("on_time", "O‘z vaqtida", BOOL),
        col("driver", "Haydovchi"),
        col("vehicle", "Transport"),
        col("issue", "Muammo"),
    ]
    model = optional_model(
        "logistics", "LogisticsTask", "household", "status", "planned_date", "started_at", "delivered_at",
        "confirmed_at", "driver", "vehicle", "issue_note", "code", "received_ok",
    )
    if model is None:
        return ReportData(columns, [], {}, [("Topshiriqlar", "0")])
    qs = model.objects.filter(**_range(p.period, "created_at"))
    if _filtered(p):
        qs = qs.filter(household__in=_households(p).values("pk"))
    qs = qs.select_related("household", "driver", "vehicle").order_by("created_at", "id")
    labels = dict(model._meta.get_field("status").choices)
    rows = []
    cancelled = open_overdue = 0
    for task in qs:
        done = task.delivered_at is not None and task.status in ("DELIVERED", "CONFIRMED")
        cancelled += 1 if task.status == "CANCELLED" else 0
        if not done and task.status != "CANCELLED" and task.planned_date is not None and task.planned_date < p.today:
            open_overdue += 1
        lead = _hours(task.created_at, task.delivered_at) if done else None
        # Same rule as /api/logistics/summary (logistics.selectors.is_on_time); undelivered tasks are not judged.
        punctual = is_on_time(task.planned_date, task.delivered_at) if done else None
        driver_name = task.driver.full_name if task.driver_id else ""
        household = task.household
        rows.append(
            {
                "code": task.code or f"LG-{task.pk:06d}",
                "household": f"{household.code} {household.name}" if household is not None else "",
                "status": labels.get(task.status, task.status),
                "created_at": task.created_at,
                "planned_date": task.planned_date,
                "started_at": task.started_at,
                "delivered_at": task.delivered_at,
                "confirmed_at": task.confirmed_at,
                "lead_hours": lead,
                "on_time": punctual,
                "driver": driver_name,
                "vehicle": task.vehicle.plate_number if task.vehicle_id else "",
                # "Muammo" only when the receipt was confirmed WITH a problem (an admin's confirmation reason or
                # an owner comment on a good receipt is not a problem).
                "issue": task.issue_note if task.received_ok is False else "",
            }
        )
    # KPIs over tasks DELIVERED in the period (by delivered_at) — the definition of /api/logistics/summary.
    delivered_qs = model.objects.filter(**_range(p.period, "delivered_at"), status__in=("DELIVERED", "CONFIRMED"))
    if _filtered(p):
        delivered_qs = delivered_qs.filter(household__in=_households(p).values("pk"))
    delivered_rows = list(
        delivered_qs.order_by("id").values_list("planned_date", "delivered_at", "created_at", "driver__full_name")
    )
    kpis = delivery_kpis(row[:3] for row in delivered_rows)
    avg_lead = _hours_avg(kpis["lead_hours"]) if kpis["lead_hours"] else None
    summary = [
        ("Topshiriqlar", str(len(rows))),
        ("Yetkazilgan", str(kpis["delivered"])),
        ("Bekor qilingan", str(cancelled)),
        ("O‘z vaqtida", _percent(metrics.percent(kpis["on_time"], kpis["judged"]))),
        ("Muddati o‘tgan (ochiq)", str(open_overdue)),
        ("O‘rtacha muddat, soat", _dec(avg_lead, 1)),
    ]
    drivers: dict[str, list] = defaultdict(list)
    for planned_date, delivered_at, created_at, driver_name in delivered_rows:
        drivers[driver_name or "—"].append((planned_date, delivered_at, created_at))
    for name in sorted(drivers):
        driver_kpis = delivery_kpis(drivers[name])
        driver_pct = _percent(metrics.percent(driver_kpis["on_time"], driver_kpis["judged"]))
        summary.append((f"Haydovchi: {name}", f"{driver_kpis['delivered']} ta yetkazdi, o‘z vaqtida {driver_pct}"))
    totals = {"lead_hours": avg_lead}
    return ReportData(columns, rows, totals, summary)


# --- 9. vet_status ---------------------------------------------------------------------------------


def vet_status(p: ReportParams) -> ReportData:
    columns = [
        col("code", "Kod"),
        col("household", "Xonadon"),
        col("district", "Tuman"),
        col("visits_done", "Tekshiruv: o‘tkazilgan", INT),
        col("visits_planned", "Tekshiruv: rejada", INT),
        col("visits_overdue", "Tekshiruv: muddati o‘tgan", INT),
        col("vacc_done", "Emlash: o‘tkazilgan", INT),
        col("vacc_planned", "Emlash: rejada", INT),
        col("vacc_overdue", "Emlash: muddati o‘tgan", INT),
        col("open_diseases", "Ochiq kasalliklar", INT),
        col("last_visit", "Oxirgi tekshiruv", DATE),
        col("last_health", "Oxirgi holat"),
    ]
    metas = queries.household_meta(_households(p))
    ids = [meta.id for meta in metas]
    period, today = p.period, p.today
    visits: dict[int, dict[str, int]] = {}
    vaccinations: dict[int, dict[str, int]] = {}
    last: dict[int, tuple] = {}
    visit_model = optional_model("vet", "VetVisit", "household", "status", "planned_date", "done_at", "health_status")
    if visit_model is not None and ids:
        for row in (
            visit_model.objects.filter(household_id__in=ids)
            .order_by()
            .values("household_id")
            .annotate(
                done=Count("id", filter=Q(status="DONE", **_range(period, "done_at"))),
                planned=Count("id", filter=Q(status="PLANNED", planned_date__gte=today)),
                overdue=Count("id", filter=Q(status="PLANNED", planned_date__lt=today)),
            )
        ):
            visits[row["household_id"]] = row
        labels = dict(visit_model._meta.get_field("health_status").choices)
        for household_id, done_at, health in (
            visit_model.objects.filter(household_id__in=ids, status="DONE", done_at__isnull=False)
            .order_by("household_id", "-done_at", "-id")
            .values_list("household_id", "done_at", "health_status")
        ):
            if household_id not in last:
                last[household_id] = (timezone.localdate(done_at), labels.get(health, health or ""))
    vacc_model = optional_model("vet", "Vaccination", "household", "status", "planned_date", "done_date")
    if vacc_model is not None and ids:
        for row in (
            vacc_model.objects.filter(household_id__in=ids)
            .order_by()
            .values("household_id")
            .annotate(
                done=Count("id", filter=Q(status="DONE", done_date__gte=period.date_from, done_date__lte=period.date_to)),
                planned=Count("id", filter=Q(status="PLANNED", planned_date__gte=today)),
                overdue=Count("id", filter=Q(status="PLANNED", planned_date__lt=today)),
            )
        ):
            vaccinations[row["household_id"]] = row
    diseases = _open_diseases(ids)
    rows = []
    zero = {"done": 0, "planned": 0, "overdue": 0}
    for meta in metas:
        v = visits.get(meta.id, zero)
        c = vaccinations.get(meta.id, zero)
        last_day, last_health = last.get(meta.id, (None, ""))
        if not meta.is_active and not any((v["done"], v["planned"], v["overdue"], c["done"], c["planned"], c["overdue"])):
            continue
        rows.append(
            {
                "code": meta.code,
                "household": meta.name,
                "district": meta.district,
                "visits_done": v["done"],
                "visits_planned": v["planned"],
                "visits_overdue": v["overdue"],
                "vacc_done": c["done"],
                "vacc_planned": c["planned"],
                "vacc_overdue": c["overdue"],
                "open_diseases": diseases.get(meta.id, 0),
                "last_visit": last_day,
                "last_health": last_health,
            }
        )
    keys = ("visits_done", "visits_planned", "visits_overdue", "vacc_done", "vacc_planned", "vacc_overdue", "open_diseases")
    totals = {key: sum(row[key] for row in rows) for key in keys}
    summary = [
        ("Xonadonlar", str(len(rows))),
        ("Muddati o‘tgan tekshiruvlar", str(totals["visits_overdue"])),
        ("Muddati o‘tgan emlashlar", str(totals["vacc_overdue"])),
        ("Ochiq kasalliklar", str(totals["open_diseases"])),
    ]
    return ReportData(columns, rows, totals, summary)


# --- 10. household_results -------------------------------------------------------------------------


def household_results(p: ReportParams) -> ReportData:
    scope = Scope(p)
    days = p.period.days
    ids = [meta.id for meta in scope.metas]
    outs = dict(
        StockTransaction.objects.filter(
            type=StockTransactionType.OUT,
            household_id__in=ids,
            **queries.stock_range(p.period.date_from, p.period.date_to),
        )
        .exclude(returned_task_out())
        .order_by()
        .values("household_id")
        .annotate(n=Count("id"))
        .values_list("household_id", "n")
    ) if ids else {}
    vaccinations: dict[int, int] = {}
    vacc_model = optional_model("vet", "Vaccination", "household", "status", "done_date")
    if vacc_model is not None and ids:
        vaccinations = dict(
            vacc_model.objects.filter(
                household_id__in=ids, status="DONE", done_date__gte=p.period.date_from, done_date__lte=p.period.date_to
            )
            .order_by()
            .values("household_id")
            .annotate(n=Count("id"))
            .values_list("household_id", "n")
        )
    rows = []
    for meta in scope.metas:
        state = scope.stats.states[meta.id]
        totals = state.current
        birds = scope.birds(state, totals)
        rows.append(
            {
                "code": meta.code,
                "household": meta.name,
                "district": meta.district,
                "birds_end": birds.birds_end,
                "eggs": totals.eggs,
                "laying_rate_pct": metrics.laying_rate_pct(totals.rl_eggs, totals.rl_layer_days),
                "feed_kg": _kg(totals.feed_cents),
                "feed_per_egg_g": metrics.feed_per_egg_g(totals.rl_feed_cents, totals.rl_eggs),
                "deaths": birds.deaths,
                "mortality_pct": metrics.mortality_pct(birds.deaths, birds.bird_days, days),
                "discipline_pct": metrics.discipline_pct(totals.reported_expected, totals.expected),
                "stock_out": outs.get(meta.id, 0),
                "vaccinations": vaccinations.get(meta.id, 0),
            }
        )
    current = scope.stats.total_current()
    bird_days, deaths = _bird_numbers(scope, scope.stats, current)
    if p.poultry_type:
        _count, merged = scope.stats.type_totals(p.poultry_type)
        birds_end = merged.birds_end
    else:
        birds_end = current.birds_end
    totals = {
        "birds_end": birds_end,
        "eggs": current.eggs,
        "laying_rate_pct": metrics.laying_rate_pct(current.rl_eggs, current.rl_layer_days),
        "feed_kg": _kg(current.feed_cents),
        "feed_per_egg_g": metrics.feed_per_egg_g(current.rl_feed_cents, current.rl_eggs),
        "deaths": deaths,
        "mortality_pct": metrics.mortality_pct(deaths, bird_days, days),
        "discipline_pct": metrics.discipline_pct(current.reported_expected, current.expected),
        "stock_out": sum(outs.values()),
        "vaccinations": sum(vaccinations.values()),
    }
    summary = [
        ("Xonadonlar", str(len(rows))),
        ("Tuxum", _num(current.eggs)),
        ("Tuxumdorlik", _percent(totals["laying_rate_pct"])),
        ("O‘lim", _percent(totals["mortality_pct"])),
    ]
    columns = [
        col("code", "Kod"),
        col("household", "Xonadon"),
        col("district", "Tuman"),
        col("birds_end", "Parranda (oxirida)", INT),
        col("eggs", "Tuxum", INT),
        col("laying_rate_pct", "Tuxumdorlik %", PERCENT, 1),
        col("feed_kg", "Yem kg", DECIMAL, 2),
        col("feed_per_egg_g", "Yem/tuxum g", DECIMAL, 1),
        col("deaths", "Nobud", INT),
        col("mortality_pct", "O‘lim %", PERCENT, 1),
        col("discipline_pct", "Intizom %", PERCENT, 1),
        col("stock_out", "Olingan mahsulotlar", INT),
        col("vaccinations", "Emlashlar", INT),
    ]
    return ReportData(columns, rows, totals, summary)


# --- summary helpers -------------------------------------------------------------------------------


def _num(value) -> str:
    from common.formatting import fmt_number

    return fmt_number(value)


def _percent(value) -> str:
    rounded = metrics.round_half_up(value, 1)
    return "—" if rounded is None else f"{rounded:.1f}%"


def _dec(value, places: int = 2) -> str:
    rounded = metrics.round_half_up(value, places)
    return "—" if rounded is None else f"{rounded:,.{places}f}".replace(",", " ")

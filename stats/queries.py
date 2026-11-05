"""Aggregate SQL queries behind the stats endpoints (a fixed number per endpoint, §7.1).

Decimal amounts (feed, stock quantities) are summed as integer hundredths (``ROUND(x × 100)``) so the
sums are exact on SQLite (which stores decimals as REAL) and on PostgreSQL alike.
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

from django.conf import settings
from django.db import connection, connections
from django.db.models import BigIntegerField, Case, DateField, F, Func, IntegerField, Q, Sum, Value, When
from django.db.models.functions import Cast, Coalesce, Round, TruncDate
from django.utils import timezone

from common.periods import day_range
from farm.models import DailyReport, Household, MovementType, PoultryBatch, PoultryMovement

from .flock import HouseholdMeta

_INT = IntegerField()
_ZERO = Value(0)


def cents(field_name: str) -> Cast:
    """Exact integer hundredths of a decimal column."""
    return Cast(Round(F(field_name) * Value(Decimal("100"))), output_field=BigIntegerField())


def cents_or(field_name: str, default: int = 0) -> Coalesce:
    """Exact integer hundredths of a nullable decimal column (``default`` for NULL)."""
    return Coalesce(cents(field_name), Value(default), output_field=BigIntegerField())


def int_sum(expression, condition: Q | None = None) -> Coalesce:
    return Coalesce(Sum(expression, filter=condition), _ZERO, output_field=BigIntegerField())


HOUSEHOLD_DELTA = Case(
    When(type=MovementType.DISTRIBUTE, then=F("quantity")),
    default=-F("quantity"),
    output_field=_INT,
)
CENTER_DELTA = Case(
    When(type=MovementType.DISTRIBUTE, then=-F("quantity")),
    When(type=MovementType.RETURN, then=F("quantity")),
    When(Q(type__in=[MovementType.DEATH, MovementType.OUT]) & Q(household__isnull=True), then=-F("quantity")),
    default=_ZERO,
    output_field=_INT,
)


def local_tz() -> ZoneInfo:
    return ZoneInfo(settings.TIME_ZONE)


def constant_utc_offset(date_from: date, date_to: date) -> int | None:
    """UTC offset in seconds if it is the same at every local midnight of ``date_from … date_to + 1``."""
    tz = local_tz()
    offsets = {
        int(datetime.combine(date_from + timedelta(days=offset), time.min, tzinfo=tz).utcoffset().total_seconds())
        for offset in range((date_to - date_from).days + 2)
    }
    return offsets.pop() if len(offsets) == 1 else None


def local_day(field_name: str = "created_at", *, date_from: date | None = None, date_to: date | None = None):
    """Local (Asia/Tashkent) calendar day of a datetime column.

    On SQLite Django evaluates ``TruncDate(tzinfo=…)`` with a Python function per row; when the zone's
    UTC offset is constant over the period (Asia/Tashkent: always +05:00) the native
    ``date(column, '+18000 seconds')`` gives the same day and is an order of magnitude faster.
    """
    if connection.vendor == "sqlite" and date_from is not None and date_to is not None:
        offset = constant_utc_offset(date_from, date_to)
        if offset is not None:
            return Func(F(field_name), Value(f"{offset:+d} seconds"), function="date", output_field=DateField())
    return TruncDate(field_name, tzinfo=local_tz())


def generated_at() -> str:
    return timezone.localtime(timezone.now()).replace(microsecond=0).isoformat()


# --- Households and movements ---------------------------------------------------------------


def household_meta(qs=None) -> list[HouseholdMeta]:
    qs = Household.objects.all() if qs is None else qs
    rows = qs.order_by("id").values_list("id", "name", "region", "district", "is_active", "created_at")
    return [
        HouseholdMeta(
            id=pk,
            name=name,
            region=region,
            district=district,
            is_active=is_active,
            created_day=timezone.localdate(created_at),
        )
        for pk, name, region, district, is_active, created_at in rows
    ]


def meta_of(household: Household) -> HouseholdMeta:
    return HouseholdMeta(
        id=household.pk,
        name=household.name,
        region=household.region,
        district=household.district,
        is_active=household.is_active,
        created_day=timezone.localdate(household.created_at),
    )


def movement_opening(start: date, household_id: int | None = None) -> list[tuple]:
    """Q-a: net household and center effect per ``(household, poultry type)`` of movements before ``start``."""
    qs = PoultryMovement.objects.filter(date__lt=start)
    if household_id is not None:
        qs = qs.filter(household_id=household_id)
    return list(
        qs.order_by()
        .values("household_id", "batch__poultry_type")
        .annotate(household_net=int_sum(HOUSEHOLD_DELTA), center_net=int_sum(CENTER_DELTA))
        .values_list("household_id", "batch__poultry_type", "household_net", "center_net")
    )


def movement_daily(start: date, end: date, household_id: int | None = None) -> list[tuple]:
    """Q-b: per ``(household, poultry type, date)`` net effects and the sums of each movement type."""
    qs = PoultryMovement.objects.filter(date__gte=start, date__lte=end)
    if household_id is not None:
        qs = qs.filter(household_id=household_id)
    return list(
        qs.order_by()
        .values("household_id", "batch__poultry_type", "date")
        .annotate(
            household_net=int_sum(HOUSEHOLD_DELTA),
            center_net=int_sum(CENTER_DELTA),
            deaths=int_sum("quantity", Q(type=MovementType.DEATH)),
            distributed=int_sum("quantity", Q(type=MovementType.DISTRIBUTE)),
            returned=int_sum("quantity", Q(type=MovementType.RETURN)),
            out=int_sum("quantity", Q(type=MovementType.OUT)),
        )
        .values_list(
            "household_id",
            "batch__poultry_type",
            "date",
            "household_net",
            "center_net",
            "deaths",
            "distributed",
            "returned",
            "out",
        )
    )


class DayIndex(Func):
    """``(date_column − start)`` in days, computed by the database (no per-row date parsing in Python)."""

    output_field = IntegerField()

    def __init__(self, field_name: str, start: date) -> None:
        super().__init__(F(field_name), Value(start.isoformat()))

    def as_sql(self, compiler, connection, **extra_context):  # PostgreSQL: date − date = integer days
        return super().as_sql(
            compiler, connection, template="(%(expressions)s AS date))", arg_joiner=" - CAST(", **extra_context
        )

    def as_sqlite(self, compiler, connection, **extra_context):
        # julianday() of two plain dates differ by an exact whole number.
        return super().as_sql(
            compiler,
            connection,
            template="CAST(julianday(%(expressions)s) AS integer)",
            arg_joiner=") - julianday(",
            **extra_context,
        )


def integer_rows(qs) -> list[tuple]:
    """Execute an ORM-built ``values_list`` query whose columns are ALL integers and return raw tuples.

    The SQL and its parameters come from the ORM compiler (quoting/parameters unchanged); only Django's
    per-row converters are skipped, which they are not needed for integers (~35 % faster on 70k rows).
    """
    sql, params = qs.query.sql_with_params()
    with connections[qs.db].cursor() as cursor:
        cursor.execute(sql, params)
        return cursor.fetchall()


#: ``weight`` column of :func:`report_rows` when the report states no average weight.
NO_WEIGHT = -1


def report_rows(start: date, end: date, household_id: int | None = None) -> list[tuple[int, int, int, int, int]]:
    """Q-c: ``(household_id, day index from start, meat, feed, avg weight)`` of every report.

    Amounts are integer hundredths (kg / g); a missing ``meat_kg`` is 0, a missing ``avg_weight_g`` is
    :data:`NO_WEIGHT`.
    """
    qs = DailyReport.objects.filter(date__gte=start, date__lte=end)
    if household_id is not None:
        qs = qs.filter(household_id=household_id)
    return integer_rows(
        qs.order_by().values_list(
            "household_id",
            DayIndex("date", start),
            cents_or("meat_kg"),
            cents("feed_kg"),
            cents_or("avg_weight_g", NO_WEIGHT),
        )
    )


def batches_initial_through(day: date) -> int:
    return PoultryBatch.objects.filter(arrived_at__lte=day).aggregate(v=int_sum("initial_quantity"))["v"]


def stock_range(date_from: date, date_to: date) -> dict:
    """``created_at`` filter for the half-open local-day range of the period (index friendly)."""
    start, end = day_range(date_from, date_to)
    return {"created_at__gte": start, "created_at__lt": end}


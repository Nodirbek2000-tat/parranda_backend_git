"""Aggregates for the two dashboards (API contract, section 3 / Dashboards)."""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal
from typing import Any

from django.db.models import Count, DecimalField, F, IntegerField, Q, Sum, Value
from django.db.models.functions import Coalesce
from django.utils import timezone

from dashboard import problems
from farm import selectors
from farm.models import DailyReport, Household, MovementType, PoultryMovement
from farm.serializers import (
    DailyReportSerializer,
    HouseholdSerializer,
    HouseholdShortSerializer,
    MovementSerializer,
)
from logistics import selectors as logistics_selectors
from orders import selectors as order_selectors
from vet import selectors as vet_selectors
from warehouse.models import Product, ProductCategory, StockTransaction, StockTransactionType, Unit, returned_task_out
from warehouse.serializers import StockTransactionSerializer, stock_files_map, task_order_ids, with_lots

PRODUCTION_DAYS = 14
PRODUCTION_MONTHS = 12
WITHOUT_REPORT_LIMIT = 10
RECENT_MOVEMENTS = 8
RECENT_REPORTS = 7
RECEIVED_PRODUCTS = 5

_INT_ZERO = Value(0)
_DEC_ZERO = Value(Decimal("0"))


def _window_start(today: date, days: int) -> date:
    """First day of a window of ``days`` calendar days ending today (inclusive)."""
    return today - timedelta(days=days - 1)


def _int_sum(qs, field: str) -> int:
    return qs.aggregate(v=Coalesce(Sum(field), _INT_ZERO, output_field=IntegerField()))["v"]


def _dec_sum(qs, field: str) -> float:
    value = qs.aggregate(
        v=Coalesce(Sum(field), _DEC_ZERO, output_field=DecimalField(max_digits=14, decimal_places=2))
    )["v"]
    return float(value)


def admin_dashboard() -> dict[str, Any]:
    today = timezone.localdate()
    last_30 = _window_start(today, 30)
    reports_today = DailyReport.objects.filter(date=today)

    reported_ids = reports_today.values("household_id")
    without_report = Household.objects.filter(is_active=True).exclude(pk__in=reported_ids).order_by("id")

    low_stock = Product.objects.filter(is_active=True, balance__lte=F("min_stock")).order_by("name")
    # One aggregate each for movements, households and today's reports (V3 §17.3 budget ≤ 22 for the endpoint).
    death_window = Q(type=MovementType.DEATH, date__gte=last_30, date__lte=today)
    deaths = PoultryMovement.objects.filter(date__gte=last_30).aggregate(
        households=Coalesce(
            Sum("quantity", filter=death_window & Q(household__isnull=False)), _INT_ZERO, output_field=IntegerField()
        ),
        center=Coalesce(
            Sum("quantity", filter=death_window & Q(household__isnull=True)), _INT_ZERO, output_field=IntegerField()
        ),
        distributed=Coalesce(
            Sum("quantity", filter=Q(type=MovementType.DISTRIBUTE)), _INT_ZERO, output_field=IntegerField()
        ),
    )
    deaths_households, deaths_center = deaths["households"], deaths["center"]
    household_counts = Household.objects.aggregate(total=Count("id"), active=Count("id", filter=Q(is_active=True)))
    today_totals = reports_today.aggregate(
        feed=Coalesce(Sum("feed_kg"), _DEC_ZERO, output_field=DecimalField(max_digits=14, decimal_places=2)),
        meat=Coalesce(Sum("meat_kg"), _DEC_ZERO, output_field=DecimalField(max_digits=14, decimal_places=2)),
        eggs=Coalesce(Sum("eggs"), _INT_ZERO, output_field=IntegerField()),
        active_reports=Count("id", filter=Q(household__is_active=True)),
    )
    birds_in_households = selectors.birds_in_households()
    birds_in_center = selectors.birds_in_center()
    stock = warehouse_summary()
    recent = PoultryMovement.objects.select_related("batch", "household", "created_by").order_by("-date", "-id")

    return {
        "households_total": household_counts["total"],
        "households_active": household_counts["active"],
        "birds_in_households": birds_in_households,
        "birds_in_center": birds_in_center,
        # TZ §4 "Jami parranda soni": households + center.
        "birds_total": birds_in_households + birds_in_center,
        "distributed_30d": deaths["distributed"],
        # Total (households + center) and its split; /stats "Nobud" = the household part only.
        "deaths_30d": deaths_households + deaths_center,
        "deaths_30d_households": deaths_households,
        "deaths_30d_center": deaths_center,
        "feed_stock_kg": stock["feed_stock_kg"],
        "feed_used_today": float(today_totals["feed"]),
        "eggs_today": today_totals["eggs"],
        # TZ §4 "Kunlik tuxum yoki go‘sht ishlab chiqarish".
        "meat_today": float(today_totals["meat"]),
        # Same population as households_active, so "reports_today / households_active" and
        # households_active - reports_today (= households without a report) stay consistent.
        "reports_today": today_totals["active_reports"],
        "households_without_report_today": HouseholdShortSerializer(
            without_report[:WITHOUT_REPORT_LIMIT], many=True
        ).data,
        "low_stock": [
            {
                "id": p.pk,
                "name": p.name,
                "balance": float(p.balance),
                "min_stock": float(p.min_stock),
                "unit": p.unit,
            }
            for p in low_stock
        ],
        "recent_movements": MovementSerializer(recent[:RECENT_MOVEMENTS], many=True).data,
        "production_14d": production(today, PRODUCTION_DAYS),
        # TZ §4 "Oylik ishlab chiqarish dinamikasi": 12 calendar months, oldest first.
        "production_12m": monthly_production(today, PRODUCTION_MONTHS),
        # TZ §4 "Ombor qoldig‘i": warehouse summary tile.
        "warehouse_summary": {key: value for key, value in stock.items() if key != "feed_stock_kg"},
        # V3 §10.1 (orders team): "orders" + "pending_orders"; zeros until the orders tables are migrated.
        **order_selectors.admin_dashboard_block(),
        # V3 §10.1 (logistics): "logistics" counters; zeros until the logistics tables are migrated.
        **logistics_selectors.admin_dashboard_block(),
        # V3 §10.1 (vet team): "vet" counters; zeros until the vet tables are migrated.
        **vet_selectors.admin_dashboard_block(),
        # V3 §10.1 (reports/dashboard team): "problem_households" + "expiring_lots".
        **problems.admin_dashboard_block(today),
    }


def production(today: date, days: int, household: Household | None = None) -> list[dict[str, Any]]:
    """Eggs and feed per day for ``days`` days ending today, oldest first, zero-filled."""
    start = _window_start(today, days)
    qs = DailyReport.objects.filter(date__gte=start, date__lte=today)
    if household is not None:
        qs = qs.filter(household=household)
    totals = {
        row["date"]: row
        for row in qs.values("date").annotate(eggs_sum=Sum("eggs"), feed_sum=Sum("feed_kg"), meat_sum=Sum("meat_kg"))
    }
    result = []
    for offset in range(days):
        day = start + timedelta(days=offset)
        row = totals.get(day)
        result.append(
            {
                "date": day.isoformat(),
                "eggs": int(row["eggs_sum"]) if row else 0,
                "feed_kg": float(row["feed_sum"]) if row else 0.0,
                "meat_kg": float(row["meat_sum"] or 0) if row else 0.0,
            }
        )
    return result


def _month_start(day: date, back: int) -> date:
    """First day of the month ``back`` months before ``day``'s month."""
    index = day.year * 12 + (day.month - 1) - back
    return date(index // 12, index % 12 + 1, 1)


def monthly_production(today: date, months: int, household: Household | None = None) -> list[dict[str, Any]]:
    """Eggs, meat and feed per calendar month (``months`` months up to the current one), zero-filled. One query."""
    from django.db.models.functions import TruncMonth

    start = _month_start(today, months - 1)
    qs = DailyReport.objects.filter(date__gte=start, date__lte=today)
    if household is not None:
        qs = qs.filter(household=household)
    rows = {
        row["month"]: row
        for row in qs.annotate(month=TruncMonth("date"))
        .order_by()
        .values("month")
        .annotate(eggs_sum=Sum("eggs"), feed_sum=Sum("feed_kg"), meat_sum=Sum("meat_kg"), reports=Count("id"))
    }
    result = []
    for back in range(months - 1, -1, -1):
        month = _month_start(today, back)
        row = rows.get(month)
        result.append(
            {
                "month": month.strftime("%Y-%m"),
                "eggs": int(row["eggs_sum"] or 0) if row else 0,
                "meat_kg": float(row["meat_sum"] or 0) if row else 0.0,
                "feed_kg": float(row["feed_sum"] or 0) if row else 0.0,
                "reports": row["reports"] if row else 0,
            }
        )
    return result


def warehouse_summary() -> dict[str, Any]:
    """Active products, low / out of stock counts and the feed stock (one aggregate)."""
    totals = Product.objects.filter(is_active=True).aggregate(
        feed=Coalesce(
            Sum("balance", filter=Q(category=ProductCategory.FEED, unit=Unit.KG)),
            _DEC_ZERO,
            output_field=DecimalField(max_digits=14, decimal_places=2),
        ),
        active=Count("id"),
        low=Count("id", filter=Q(balance__lte=F("min_stock"), balance__gt=0)),
        out=Count("id", filter=Q(balance__lte=0)),
        reserved=Count("id", filter=Q(reserved__gt=0)),
    )
    return {
        "active_products": totals["active"],
        "low_stock": totals["low"],
        "out_of_stock": totals["out"],
        "with_reservation": totals["reserved"],
        "feed_stock_kg": float(totals["feed"]),
    }


def owner_dashboard(household: Household) -> dict[str, Any]:
    today = timezone.localdate()
    last_7 = _window_start(today, 7)
    last_30 = _window_start(today, 30)
    reports = DailyReport.objects.filter(household=household).select_related("household", "created_by")
    week = reports.filter(date__gte=last_7, date__lte=today)
    annotated = selectors.households_with_counts(Household.objects.select_related("owner")).get(pk=household.pk)
    today_report = reports.filter(date=today).first()
    received = list(
        with_lots(
            StockTransaction.objects.filter(household=household, type=StockTransactionType.OUT)
            .exclude(returned_task_out())
            .select_related("product", "household", "created_by")
            .order_by("-created_at", "-id")
        )[:RECEIVED_PRODUCTS]
    )
    return {
        "household": HouseholdSerializer(annotated).data,
        "bird_count": annotated.bird_count,
        "today_report": DailyReportSerializer(today_report).data if today_report else None,
        "eggs_7d": _int_sum(week, "eggs"),
        "feed_7d": _dec_sum(week, "feed_kg"),
        "meat_7d": _dec_sum(week, "meat_kg"),
        "deaths_7d": _int_sum(
            PoultryMovement.objects.filter(household=household, type=MovementType.DEATH, date__gte=last_7, date__lte=today),
            "quantity",
        ),
        "deaths_30d": _int_sum(
            PoultryMovement.objects.filter(household=household, type=MovementType.DEATH, date__gte=last_30),
            "quantity",
        ),
        "batches": selectors.household_batches(household.pk),
        "recent_reports": DailyReportSerializer(reports.order_by("-date", "-id")[:RECENT_REPORTS], many=True).data,
        # Owner view (V3 §5.4): task references link to /my/orders/{id}, never to staff pages.
        "received_products": StockTransactionSerializer(
            received,
            many=True,
            context={"owner_view": True, "task_orders": task_order_ids(received), "files_map": stock_files_map(received)},
        ).data,
        # V3 §10.2 (orders team): {"open", "awaiting_confirmation"}; zeros until the orders tables are migrated.
        "orders": order_selectors.owner_counts(household),
        # V3 §10.2 (logistics): own DELIVERED tasks waiting for "Qabul qildim"; empty until migrated.
        **logistics_selectors.owner_dashboard_block(household.owner),
        # V3 §10.2 (vet team): {"next_visit", "next_vaccination", "open_requests"}; empty until migrated.
        "vet": vet_selectors.owner_dashboard_block(household),
    }

"""OpenAPI shapes of the two dashboards (API_CONTRACT.md §3 Dashboards)."""

from __future__ import annotations

from rest_framework import serializers

from farm.serializers import (
    DailyReportSerializer,
    HouseholdBatchSerializer,
    HouseholdSerializer,
    HouseholdShortSerializer,
    MovementSerializer,
)
from warehouse.models import Unit
from warehouse.serializers import StockLotSerializer, StockTransactionSerializer


class DashboardLowStockSerializer(serializers.Serializer):
    id = serializers.IntegerField()
    name = serializers.CharField()
    balance = serializers.FloatField()
    min_stock = serializers.FloatField()
    unit = serializers.ChoiceField(choices=Unit.choices)


class ProductionDaySerializer(serializers.Serializer):
    date = serializers.DateField()
    eggs = serializers.IntegerField()
    feed_kg = serializers.FloatField()
    meat_kg = serializers.FloatField()


class ProductionMonthSerializer(serializers.Serializer):
    month = serializers.CharField(help_text="YYYY-MM")
    eggs = serializers.IntegerField()
    meat_kg = serializers.FloatField()
    feed_kg = serializers.FloatField()
    reports = serializers.IntegerField()


class WarehouseSummarySerializer(serializers.Serializer):
    active_products = serializers.IntegerField()
    low_stock = serializers.IntegerField(help_text="0 < balance ≤ min_stock")
    out_of_stock = serializers.IntegerField(help_text="balance ≤ 0")
    with_reservation = serializers.IntegerField()


class ProblemReasonSerializer(serializers.Serializer):
    code = serializers.CharField(help_text="NO_REPORT | HIGH_MORTALITY | OVERDUE_VACCINATION | OPEN_DISEASE")
    label = serializers.CharField()
    value = serializers.FloatField()


class ProblemHouseholdSerializer(serializers.Serializer):
    household = HouseholdShortSerializer()
    district = serializers.CharField()
    reasons = ProblemReasonSerializer(many=True)


class ProblemHouseholdsSerializer(serializers.Serializer):
    """V3 §10.1 ``problem_households`` (``items`` ≤ 10, ``count`` = all)."""

    count = serializers.IntegerField()
    items = ProblemHouseholdSerializer(many=True)


class ExpiringLotsSerializer(serializers.Serializer):
    """V3 §10.1 ``expiring_lots`` (open lots expired or expiring within ``expiry_reminder_days``; ≤ 5)."""

    count = serializers.IntegerField()
    items = StockLotSerializer(many=True)


class AdminDashboardSerializer(serializers.Serializer):
    households_total = serializers.IntegerField()
    households_active = serializers.IntegerField()
    birds_in_households = serializers.IntegerField()
    birds_in_center = serializers.IntegerField()
    birds_total = serializers.IntegerField()
    distributed_30d = serializers.IntegerField()
    deaths_30d = serializers.IntegerField(help_text="All DEATH movements of 30 days (households + center).")
    deaths_30d_households = serializers.IntegerField()
    deaths_30d_center = serializers.IntegerField()
    feed_stock_kg = serializers.FloatField()
    feed_used_today = serializers.FloatField()
    eggs_today = serializers.IntegerField()
    meat_today = serializers.FloatField()
    reports_today = serializers.IntegerField()
    households_without_report_today = HouseholdShortSerializer(many=True)
    low_stock = DashboardLowStockSerializer(many=True)
    recent_movements = MovementSerializer(many=True)
    production_14d = ProductionDaySerializer(many=True)
    production_12m = ProductionMonthSerializer(many=True)
    warehouse_summary = WarehouseSummarySerializer()
    problem_households = ProblemHouseholdsSerializer()
    expiring_lots = ExpiringLotsSerializer()


class OwnerDashboardSerializer(serializers.Serializer):
    household = HouseholdSerializer()
    bird_count = serializers.IntegerField()
    today_report = DailyReportSerializer(allow_null=True)
    eggs_7d = serializers.IntegerField()
    feed_7d = serializers.FloatField()
    meat_7d = serializers.FloatField()
    deaths_7d = serializers.IntegerField()
    deaths_30d = serializers.IntegerField()
    batches = HouseholdBatchSerializer(many=True)
    recent_reports = DailyReportSerializer(many=True)
    received_products = StockTransactionSerializer(many=True)

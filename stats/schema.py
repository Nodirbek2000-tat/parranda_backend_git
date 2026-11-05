"""OpenAPI shapes of the stats / summary responses (used only by ``extend_schema``).

The services build plain dicts; these serializers document exactly those keys (API_CONTRACT_V2 §1, §5).
"""

from __future__ import annotations

from drf_spectacular.utils import OpenApiParameter, PolymorphicProxySerializer, extend_schema_field
from rest_framework import serializers

from farm.models import PoultryType
from farm.serializers import BatchSerializer, HouseholdBatchSerializer, HouseholdShortSerializer
from warehouse.models import ProductCategory, Unit
from warehouse.serializers import ProductSerializer

PERIOD_PARAMETERS = [
    OpenApiParameter("date_from", str, description="YYYY-MM-DD (shu kun ham kiradi). Standart: date_to − 29"),
    OpenApiParameter("date_to", str, description="YYYY-MM-DD (shu kun ham kiradi, bugundan keyin emas). Standart: bugun"),
]
THRESHOLD_PARAMETER = OpenApiParameter(
    "mortality_threshold", float, description="0…100, standart 5 (o‘lim darajasi chegarasi, %)"
)
OVERVIEW_FILTER_PARAMETERS = [
    OpenApiParameter("region", str, description="Viloyat (xonadonlar to‘plami)"),
    OpenApiParameter("district", str, description="Tuman"),
    OpenApiParameter("poultry_type", str, description="BROILER | BREEDER — shu turdagi harakati bor xonadonlar"),
    OpenApiParameter("exclude_demo", str, description="1 — demo / sinov xonadonlarisiz (TZ §18)"),
]


def _num(**kwargs) -> serializers.FloatField:
    return serializers.FloatField(**kwargs)


def _nullable_num() -> serializers.FloatField:
    return serializers.FloatField(allow_null=True)


class StatsPeriodSerializer(serializers.Serializer):
    date_from = serializers.DateField()
    date_to = serializers.DateField()
    days = serializers.IntegerField()


class StatsComparedPeriodSerializer(StatsPeriodSerializer):
    previous_from = serializers.DateField()
    previous_to = serializers.DateField()


class KpiSerializer(serializers.Serializer):
    value = _nullable_num()
    previous = _nullable_num()
    delta = _nullable_num()
    delta_unit = serializers.ChoiceField(choices=[("%", "%"), ("pp", "pp")])


class KpisSerializer(serializers.Serializer):
    birds_end = KpiSerializer()
    birds_avg = KpiSerializer()
    meat_kg = KpiSerializer(help_text="Σ meat_kg of the reports (kg).")
    feed_kg = KpiSerializer()
    deaths = KpiSerializer()
    distributed = KpiSerializer()
    returned = KpiSerializer()
    reports = KpiSerializer()
    fcr = KpiSerializer(help_text="Feed conversion ratio Σ feed_kg / Σ meat_kg (lower is better; null when no meat).")
    meat_per_bird_kg = KpiSerializer(help_text="Σ meat_kg / birds_avg (kg per average bird).")
    avg_weight_g = KpiSerializer(help_text="Bird-weighted mean of the reported avg_weight_g.")
    feed_per_bird_day_kg = KpiSerializer()
    mortality_pct = KpiSerializer()
    report_discipline_pct = KpiSerializer()


class OverviewHouseholdsSerializer(serializers.Serializer):
    total = serializers.IntegerField()
    active = serializers.IntegerField()
    with_birds = serializers.IntegerField()
    reporting = serializers.IntegerField()


class OverviewCenterSerializer(serializers.Serializer):
    birds_end = serializers.IntegerField()
    deaths = serializers.IntegerField()
    out = serializers.IntegerField()


class OverviewSeriesSerializer(serializers.Serializer):
    date = serializers.DateField()
    birds = serializers.IntegerField()
    meat_kg = _num(help_text="Σ DailyReport.meat_kg of the day.")
    feed_kg = _num()
    avg_weight_g = _nullable_num()
    fcr = _nullable_num()
    deaths = serializers.IntegerField()
    distributed = serializers.IntegerField()
    returned = serializers.IntegerField()
    reports = serializers.IntegerField(help_text="All report rows of the day (inactive / birdless households too).")
    reported_expected = serializers.IntegerField(help_text="Households due that day that reported.")
    expected_reports = serializers.IntegerField(help_text="Households due that day (today: due so far).")


class OverviewPoultryTypeSerializer(serializers.Serializer):
    poultry_type = serializers.ChoiceField(choices=PoultryType.choices)
    poultry_type_display = serializers.CharField()
    households = serializers.IntegerField()
    birds_end = serializers.IntegerField()
    birds_avg = _nullable_num()
    deaths = serializers.IntegerField()
    distributed = serializers.IntegerField()
    mortality_pct = _nullable_num()


class OverviewDistrictSerializer(serializers.Serializer):
    region = serializers.CharField()
    district = serializers.CharField()
    households = serializers.IntegerField()
    households_active = serializers.IntegerField()
    birds_end = serializers.IntegerField()
    meat_kg = _num()
    feed_kg = _num()
    fcr = _nullable_num()
    deaths = serializers.IntegerField()
    reports = serializers.IntegerField()
    mortality_pct = _nullable_num()
    report_discipline_pct = _nullable_num()


class RankedHouseholdSerializer(serializers.Serializer):
    household = HouseholdShortSerializer()
    district = serializers.CharField()
    is_active = serializers.BooleanField()
    fcr = _nullable_num()
    meat_kg = _num()
    feed_kg = _num()
    reports = serializers.IntegerField()
    avg_weight_g = _nullable_num()


class HighMortalityItemSerializer(serializers.Serializer):
    household = HouseholdShortSerializer()
    district = serializers.CharField()
    is_active = serializers.BooleanField()
    birds_avg = _nullable_num()
    deaths = serializers.IntegerField()
    mortality_pct = _nullable_num()


class HighMortalitySerializer(serializers.Serializer):
    threshold_pct = _num()
    count = serializers.IntegerField()
    items = HighMortalityItemSerializer(many=True)


class OverviewFiltersSerializer(serializers.Serializer):
    region = serializers.CharField(required=False)
    district = serializers.CharField(required=False)
    poultry_type = serializers.CharField(required=False)
    exclude_demo = serializers.CharField(required=False)


class OverviewStatsSerializer(serializers.Serializer):
    period = StatsComparedPeriodSerializer()
    generated_at = serializers.DateTimeField()
    filters = OverviewFiltersSerializer()
    households = OverviewHouseholdsSerializer()
    kpis = KpisSerializer()
    center = OverviewCenterSerializer()
    series = OverviewSeriesSerializer(many=True)
    by_poultry_type = OverviewPoultryTypeSerializer(many=True)
    by_district = OverviewDistrictSerializer(many=True)
    top_households = RankedHouseholdSerializer(many=True)
    bottom_households = RankedHouseholdSerializer(many=True)
    high_mortality = HighMortalitySerializer()


class StatsHouseholdSerializer(serializers.Serializer):
    id = serializers.IntegerField()
    code = serializers.CharField()
    name = serializers.CharField()
    is_active = serializers.BooleanField()
    region = serializers.CharField()
    district = serializers.CharField()


class DisciplineSerializer(serializers.Serializer):
    expected = serializers.IntegerField()
    reported = serializers.IntegerField()
    missing = serializers.IntegerField()


class HouseholdSeriesSerializer(serializers.Serializer):
    date = serializers.DateField()
    birds = serializers.IntegerField()
    meat_kg = _num(help_text="Σ DailyReport.meat_kg of the day.")
    feed_kg = _num()
    avg_weight_g = _nullable_num()
    fcr = _nullable_num()
    deaths = serializers.IntegerField()
    distributed = serializers.IntegerField()
    returned = serializers.IntegerField()
    reported = serializers.BooleanField()
    expected = serializers.BooleanField()
    pending = serializers.BooleanField(help_text="Today, due and not reported yet (not counted as missing).")


class HouseholdPoultryTypeSerializer(serializers.Serializer):
    poultry_type = serializers.ChoiceField(choices=PoultryType.choices)
    poultry_type_display = serializers.CharField()
    birds_end = serializers.IntegerField()
    birds_avg = _nullable_num()
    deaths = serializers.IntegerField()
    mortality_pct = _nullable_num()


class HouseholdStatsSerializer(serializers.Serializer):
    household = StatsHouseholdSerializer()
    period = StatsComparedPeriodSerializer()
    generated_at = serializers.DateTimeField()
    kpis = KpisSerializer()
    discipline = DisciplineSerializer()
    missing_days = serializers.ListField(child=serializers.DateField())
    series = HouseholdSeriesSerializer(many=True)
    by_poultry_type = HouseholdPoultryTypeSerializer(many=True)
    batches = HouseholdBatchSerializer(many=True)


class BatchAgeSerializer(serializers.Serializer):
    age_days_at_arrival = serializers.IntegerField()
    days_since_arrival = serializers.IntegerField()
    age_days_today = serializers.IntegerField()


class BatchTotalsSerializer(serializers.Serializer):
    initial_quantity = serializers.IntegerField()
    center_stock = serializers.IntegerField()
    in_households = serializers.IntegerField()
    alive = serializers.IntegerField()
    distributed = serializers.IntegerField()
    returned = serializers.IntegerField()
    deaths_total = serializers.IntegerField()
    deaths_center = serializers.IntegerField()
    deaths_households = serializers.IntegerField()
    out_total = serializers.IntegerField()
    out_center = serializers.IntegerField()
    out_households = serializers.IntegerField()
    households_now = serializers.IntegerField()
    mortality_pct = _nullable_num()


class BatchHouseholdSerializer(serializers.Serializer):
    household = HouseholdShortSerializer()
    is_active = serializers.BooleanField()
    count_now = serializers.IntegerField()
    distributed = serializers.IntegerField()
    returned = serializers.IntegerField()
    deaths = serializers.IntegerField()
    out = serializers.IntegerField()
    mortality_pct = _nullable_num()


class BatchSeriesSerializer(serializers.Serializer):
    date = serializers.DateField()
    center_stock = serializers.IntegerField()
    in_households = serializers.IntegerField()
    distributed = serializers.IntegerField()
    returned = serializers.IntegerField()
    deaths = serializers.IntegerField()
    out = serializers.IntegerField()


class BatchStatsSerializer(serializers.Serializer):
    batch = BatchSerializer()
    generated_at = serializers.DateTimeField()
    period = StatsPeriodSerializer()
    age = BatchAgeSerializer()
    totals = BatchTotalsSerializer()
    households = BatchHouseholdSerializer(many=True)
    series = BatchSeriesSerializer(many=True)


class ProductTotalsSerializer(serializers.Serializer):
    opening_balance = _num()
    in_qty = _num()
    in_count = serializers.IntegerField()
    out_qty = _num()
    out_count = serializers.IntegerField()
    net = _num()
    adjust_in_qty = _num()
    adjust_out_qty = _num()
    closing_balance = _num()


class ProductSeriesSerializer(serializers.Serializer):
    date = serializers.DateField()
    in_qty = _num()
    out_qty = _num()
    adjust_qty = _num()
    balance = _num()


class ProductConsumerSerializer(serializers.Serializer):
    household = HouseholdShortSerializer()
    quantity = _num()
    share_pct = _nullable_num()
    transactions = serializers.IntegerField()


class ProductStatsSerializer(serializers.Serializer):
    product = ProductSerializer()
    period = StatsPeriodSerializer()
    generated_at = serializers.DateTimeField()
    totals = ProductTotalsSerializer()
    avg_daily_out = _nullable_num()
    days_of_cover = _nullable_num()
    stockout_date = serializers.DateField(allow_null=True)
    series = ProductSeriesSerializer(many=True)
    top_consumers = ProductConsumerSerializer(many=True)
    out_unassigned_qty = _num()


class WarehouseProductsSerializer(serializers.Serializer):
    total = serializers.IntegerField()
    active = serializers.IntegerField()
    low = serializers.IntegerField()


class WarehouseFeedSerializer(serializers.Serializer):
    balance_kg = _num()
    in_kg = _num()
    out_kg = _num()
    avg_daily_out_kg = _nullable_num()
    days_of_cover = _nullable_num()
    stockout_date = serializers.DateField(allow_null=True)


class WarehouseSeriesSerializer(serializers.Serializer):
    date = serializers.DateField()
    in_count = serializers.IntegerField()
    out_count = serializers.IntegerField()
    feed_in_kg = _num()
    feed_out_kg = _num()


class WarehouseCategorySerializer(serializers.Serializer):
    category = serializers.ChoiceField(choices=ProductCategory.choices)
    category_display = serializers.CharField()
    unit = serializers.ChoiceField(choices=Unit.choices)
    products = serializers.IntegerField()
    balance = _num()
    in_qty = _num()
    out_qty = _num()
    adjust_in_qty = _num()
    adjust_out_qty = _num()
    avg_daily_out = _nullable_num()
    days_of_cover = _nullable_num()


class WarehouseItemSerializer(serializers.Serializer):
    id = serializers.IntegerField()
    name = serializers.CharField()
    category = serializers.ChoiceField(choices=ProductCategory.choices)
    category_display = serializers.CharField()
    unit = serializers.ChoiceField(choices=Unit.choices)
    balance = _num()
    min_stock = _num()
    is_low = serializers.BooleanField()
    is_active = serializers.BooleanField()
    in_qty = _num()
    out_qty = _num()
    adjust_in_qty = _num()
    adjust_out_qty = _num()
    avg_daily_out = _nullable_num()
    days_of_cover = _nullable_num()
    stockout_date = serializers.DateField(allow_null=True)


class WarehouseLowStockSerializer(serializers.Serializer):
    id = serializers.IntegerField()
    name = serializers.CharField()
    unit = serializers.ChoiceField(choices=Unit.choices)
    balance = _num()
    min_stock = _num()
    avg_daily_out = _nullable_num()
    days_of_cover = _nullable_num()
    stockout_date = serializers.DateField(allow_null=True)


class WarehouseConsumerSerializer(serializers.Serializer):
    household = HouseholdShortSerializer()
    feed_kg = _num()
    share_pct = _nullable_num()
    transactions = serializers.IntegerField()


class WarehouseStatsSerializer(serializers.Serializer):
    period = StatsPeriodSerializer()
    generated_at = serializers.DateTimeField()
    products = WarehouseProductsSerializer()
    feed = WarehouseFeedSerializer()
    series = WarehouseSeriesSerializer(many=True)
    by_category = WarehouseCategorySerializer(many=True)
    items = WarehouseItemSerializer(many=True)
    low_stock = WarehouseLowStockSerializer(many=True)
    top_consumers = WarehouseConsumerSerializer(many=True)


# --- §5.1 bot summary ---------------------------------------------------------------------------


class SummaryHouseholdSerializer(serializers.Serializer):
    id = serializers.IntegerField()
    code = serializers.CharField()
    name = serializers.CharField()


class OwnerTodaySerializer(serializers.Serializer):
    report_submitted = serializers.BooleanField()
    feed_kg = serializers.FloatField(allow_null=True)
    meat_kg = serializers.FloatField(allow_null=True, help_text="Bugungi hisobotdagi go‘sht (kg)")
    avg_weight_g = serializers.FloatField(allow_null=True, help_text="Bugungi hisobotdagi o‘rtacha vazn (g)")
    deaths = serializers.IntegerField()


class OwnerWeekSerializer(serializers.Serializer):
    date_from = serializers.DateField()
    date_to = serializers.DateField()
    days = serializers.IntegerField()
    reports = serializers.IntegerField(help_text="All report rows of the 7 days.")
    reported = serializers.IntegerField(help_text="Expected days with a report (discipline numerator).")
    expected = serializers.IntegerField(help_text="Expected days (today only once reported).")
    feed_kg = _num()
    meat_kg = _num()
    fcr = _nullable_num()
    avg_weight_g = _nullable_num()
    deaths = serializers.IntegerField()


class StaffTodaySerializer(serializers.Serializer):
    households_active = serializers.IntegerField()
    households_expected = serializers.IntegerField(help_text="Active households with birds today (due).")
    households_reported = serializers.IntegerField(help_text="Due households that reported today.")
    report_discipline_pct = _nullable_num()
    feed_kg = _num()
    meat_kg = _num()
    fcr = _nullable_num()
    deaths = serializers.IntegerField(help_text="DEATH movements in households today.")
    deaths_center = serializers.IntegerField(help_text="DEATH movements in the center today.")
    birds_in_households = serializers.IntegerField()
    birds_in_center = serializers.IntegerField()
    birds_total = serializers.IntegerField()
    low_stock_count = serializers.IntegerField()


@extend_schema_field(
    PolymorphicProxySerializer(
        component_name="SummaryToday",
        serializers=[OwnerTodaySerializer, StaffTodaySerializer],
        resource_type_field_name=None,
    )
)
class SummaryTodayField(serializers.JSONField):
    """OWNER → ``OwnerToday``, ADMIN / WAREHOUSE → ``StaffToday``."""


class BotSummarySerializer(serializers.Serializer):
    """OWNER: ``household``, ``bird_count``, ``today`` (owner shape), ``last_7d``. Staff: ``today`` (staff shape)."""

    role = serializers.ChoiceField(choices=[("ADMIN", "ADMIN"), ("WAREHOUSE", "WAREHOUSE"), ("OWNER", "OWNER")])
    date = serializers.DateField()
    generated_at = serializers.DateTimeField()
    household = SummaryHouseholdSerializer(required=False)
    bird_count = serializers.IntegerField(required=False)
    today = SummaryTodayField()
    last_7d = OwnerWeekSerializer(required=False)

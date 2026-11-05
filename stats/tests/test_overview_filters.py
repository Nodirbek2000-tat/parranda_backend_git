"""Dashboard filters of /api/stats/overview (region, district, poultry type) and meat output (TZ §4)."""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

from django.utils import timezone

from common.testing import BaseAPITestCase, distribute, make_batch
from farm.models import DailyReport, Household, PoultryType


class OverviewFilterTests(BaseAPITestCase):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        Household.objects.filter(pk=cls.household_b.pk).update(region="Buxoro viloyati", district="Kogon tumani")
        broilers = make_batch(cls.admin, 400, PoultryType.BROILER)
        distribute(cls.admin, broilers, cls.household_b, 150)
        today = timezone.localdate()
        DailyReport.objects.create(household=cls.household_a, date=today - timedelta(days=1), bird_count=300,
                                   feed_kg=Decimal("30"), eggs=250, deaths=0, created_by=cls.admin)
        DailyReport.objects.create(household=cls.household_b, date=today - timedelta(days=1), bird_count=350,
                                   feed_kg=Decimal("40"), eggs=100, deaths=0, created_by=cls.admin,
                                   meat_kg=Decimal("62.5"), avg_weight_g=Decimal("2100"))

    def setUp(self):
        super().setUp()
        self.login_as(self.admin)

    def test_meat_series_and_totals(self):
        data = self.client.get("/api/stats/overview").data
        self.assertEqual(data["meat"], {"total_kg": 62.5, "previous_total_kg": 0.0, "avg_weight_g": 2100.0})
        self.assertEqual(sum(day["meat_kg"] for day in data["series"]), 62.5)
        self.assertEqual(data["filters"], {})

    def test_region_and_poultry_type_filters(self):
        everyone = self.client.get("/api/stats/overview").data
        self.assertEqual(everyone["households"]["total"], 2)
        samarqand = self.client.get("/api/stats/overview?region=Samarqand viloyati").data
        self.assertEqual(samarqand["households"]["total"], 1)
        self.assertEqual(samarqand["kpis"]["eggs"]["value"], 250)
        self.assertEqual(samarqand["meat"]["total_kg"], 0.0)
        self.assertEqual(samarqand["filters"], {"region": "Samarqand viloyati"})
        broiler = self.client.get("/api/stats/overview?poultry_type=BROILER").data
        self.assertEqual(broiler["households"]["total"], 1)
        self.assertEqual(broiler["meat"]["total_kg"], 62.5)
        district = self.client.get("/api/stats/overview?district=Kogon tumani").data
        self.assertEqual(district["households"]["total"], 1)

    def test_bad_poultry_type(self):
        response = self.client.get("/api/stats/overview?poultry_type=DUCK")
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.data, {"poultry_type": ["“DUCK” ruxsat etilgan qiymat emas."]})

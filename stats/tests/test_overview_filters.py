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
        breeders = make_batch(cls.admin, 400, PoultryType.BREEDER)
        distribute(cls.admin, breeders, cls.household_b, 150)
        today = timezone.localdate()
        DailyReport.objects.create(household=cls.household_a, date=today - timedelta(days=1), bird_count=300,
                                   feed_kg=Decimal("30"), deaths=0, created_by=cls.admin)
        DailyReport.objects.create(household=cls.household_b, date=today - timedelta(days=1), bird_count=350,
                                   feed_kg=Decimal("40"), deaths=0, created_by=cls.admin,
                                   meat_kg=Decimal("62.5"), avg_weight_g=Decimal("2100"))

    def setUp(self):
        super().setUp()
        self.login_as(self.admin)

    def test_meat_series_and_totals(self):
        data = self.client.get("/api/stats/overview").data
        kpis = data["kpis"]
        self.assertEqual(kpis["meat_kg"], {"value": 62.5, "previous": 0.0, "delta": None, "delta_unit": "%"})
        self.assertEqual(kpis["avg_weight_g"]["value"], 2100.0)  # household A stated no weight
        self.assertEqual(kpis["fcr"]["value"], 1.12)  # (30 + 40) / 62.5
        self.assertEqual(sum(day["meat_kg"] for day in data["series"]), 62.5)
        self.assertEqual(data["filters"], {})

    def test_region_and_poultry_type_filters(self):
        everyone = self.client.get("/api/stats/overview").data
        self.assertEqual(everyone["households"]["total"], 2)
        samarqand = self.client.get("/api/stats/overview?region=Samarqand viloyati").data
        self.assertEqual(samarqand["households"]["total"], 1)
        self.assertEqual(samarqand["kpis"]["feed_kg"]["value"], 30.0)
        self.assertEqual(samarqand["kpis"]["meat_kg"]["value"], 0.0)
        self.assertIsNone(samarqand["kpis"]["fcr"]["value"])
        self.assertEqual(samarqand["filters"], {"region": "Samarqand viloyati"})
        breeder = self.client.get("/api/stats/overview?poultry_type=BREEDER").data
        self.assertEqual(breeder["households"]["total"], 1)
        self.assertEqual(breeder["kpis"]["meat_kg"]["value"], 62.5)
        self.assertEqual(self.client.get("/api/stats/overview?poultry_type=BROILER").data["households"]["total"], 2)
        district = self.client.get("/api/stats/overview?district=Kogon tumani").data
        self.assertEqual(district["households"]["total"], 1)

    def test_bad_poultry_type(self):
        response = self.client.get("/api/stats/overview?poultry_type=DUCK")
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.data, {"poultry_type": ["“DUCK” ruxsat etilgan qiymat emas."]})

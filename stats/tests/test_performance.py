"""Performance guard (API_CONTRACT_V2 §7): 100 households × 365 days of reports built with bulk_create.

Asserts the §7.1 query budgets on the large dataset and a generous time ceiling (the strict < 300 ms
median target is checked by ``manage.py stats_benchmark`` on a throwaway file database).
``NNP_PRINT_TIMINGS=1`` prints the measured table.
"""

from __future__ import annotations

import os
from datetime import timedelta
from unittest import mock

from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import SimpleTestCase, override_settings

from accounts.models import User
from common.testing import BOT_API_TOKEN
from farm.models import DailyReport, Household, PoultryBatch
from warehouse.models import Product, StockTransaction

from stats.benchmark import DatasetSize, build_dataset, time_call

from .helpers import TODAY, FrozenClockTestCase

CEILING_MS = 1500  # catches pathological regressions without being flaky on a busy machine


@override_settings(BOT_API_TOKEN=BOT_API_TOKEN)
class LargeDatasetPerformanceTests(FrozenClockTestCase):
    @classmethod
    def setUpTestData(cls):
        cls.counts = build_dataset(
            DatasetSize(households=100, report_days=365, products=20, transactions_per_product_day=10, stock_days=60),
            today=TODAY,
        )
        cls.admin = User.objects.get(username="bench_admin")
        cls.household = Household.objects.select_related("owner").order_by("id").first()
        User.objects.filter(pk=cls.household.owner_id).update(telegram_id=990001, must_change_password=False)
        User.objects.filter(pk=cls.admin.pk).update(telegram_id=990002)

    def test_dataset_shape(self):
        self.assertEqual(self.counts["households"], 100)
        self.assertGreater(DailyReport.objects.count(), 34_000)  # 100 × 365 minus ~4 % missing days
        self.assertEqual(StockTransaction.objects.count(), 20 * 10 * 60)
        self.assertEqual(PoultryBatch.objects.count(), 10)
        self.assertEqual(Product.objects.count(), 20)

    def test_budgets_and_timing(self):
        batch = PoultryBatch.objects.order_by("id").first()
        product = Product.objects.order_by("id").first()
        owner = User.objects.get(pk=self.household.owner_id)
        year = f"?date_from={(TODAY - timedelta(days=365)).isoformat()}"
        bot = {"HTTP_X_BOT_TOKEN": BOT_API_TOKEN}
        plan = [
            ("stats overview", 8, self.admin, "/api/stats/overview", {}),
            ("stats overview 366 d", 8, self.admin, f"/api/stats/overview{year}", {}),
            ("household stats", 7, self.admin, f"/api/households/{self.household.pk}/stats", {}),
            ("household stats 366 d", 7, self.admin, f"/api/households/{self.household.pk}/stats{year}", {}),
            ("my stats", 7, owner, "/api/my/stats", {}),
            ("batch stats", 6, self.admin, f"/api/batches/{batch.pk}/stats", {}),
            ("product stats", 6, self.admin, f"/api/products/{product.pk}/stats", {}),
            ("warehouse stats", 6, self.admin, "/api/warehouse/stats", {}),
            ("warehouse stats 366 d", 6, self.admin, f"/api/warehouse/stats{year}", {}),
            ("bot summary owner", 10, None, "/api/bot/users/990001/summary", bot),
            ("bot summary staff", 10, None, "/api/bot/users/990002/summary", bot),
        ]
        rows = []
        for name, budget, user, url, headers in plan:
            self.client.force_authenticate(user=user)

            def call(url=url, headers=headers):
                response = self.client.get(url, **headers)
                self.assertEqual(response.status_code, 200, response.content[:300])
                return response

            timing = time_call(name, call, runs=3)
            rows.append((timing, budget))
            with self.subTest(endpoint=name):
                self.assertLessEqual(timing.queries, budget)
                self.assertLess(timing.median_ms, CEILING_MS)
        if os.environ.get("NNP_PRINT_TIMINGS"):
            print("\nendpoint (100 households x 365 days)   median ms  queries  budget")
            for timing, budget in rows:
                print(f"{timing.name:<38}{timing.median_ms:>10.1f}{timing.queries:>9}{budget:>8}")

    def test_numbers_are_consistent_at_scale(self):
        self.client.force_authenticate(user=self.admin)
        data = self.client.get("/api/stats/overview").json()
        series_meat = round(sum(row["meat_kg"] for row in data["series"]), 2)
        self.assertEqual(series_meat, data["kpis"]["meat_kg"]["value"])
        self.assertGreater(series_meat, 0)
        self.assertEqual(sum(row["reports"] for row in data["series"]), data["kpis"]["reports"]["value"])
        self.assertEqual(data["series"][-1]["birds"], data["kpis"]["birds_end"]["value"])
        self.assertEqual(sum(row["households"] for row in data["by_district"]), 100)
        self.assertEqual(round(sum(row["meat_kg"] for row in data["by_district"]), 2), series_meat)
        self.assertIsNotNone(data["kpis"]["fcr"]["value"])
        self.assertIsNotNone(data["kpis"]["avg_weight_g"]["value"])
        expected = sum(row["expected_reports"] for row in data["series"])
        self.assertGreater(expected, 0)
        self.assertLessEqual(data["kpis"]["report_discipline_pct"]["value"], 100.0)


class BenchmarkGuardTests(SimpleTestCase):
    def test_refuses_without_throwaway_db(self):
        with mock.patch.dict(os.environ, {"DB_PATH": ""}), self.assertRaises(CommandError):
            call_command("stats_benchmark")
        from django.conf import settings

        with mock.patch.dict(os.environ, {"DB_PATH": str(settings.BASE_DIR / "db.sqlite3")}), \
                self.assertRaises(CommandError) as ctx:
            call_command("stats_benchmark")
        self.assertIn("db.sqlite3", str(ctx.exception))



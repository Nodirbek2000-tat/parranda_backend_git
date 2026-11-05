from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

from django.utils import timezone

from common.testing import BaseAPITestCase, make_product
from farm import services as farm_services
from warehouse import services as warehouse_services


class DashboardTests(BaseAPITestCase):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        today = timezone.localdate()
        farm_services.create_daily_report(
            actor=cls.owner_a, household=None, feed_kg=Decimal("20.5"), eggs=200, deaths=2
        )
        farm_services.create_daily_report(
            actor=cls.owner_a, household=None, on_date=today - timedelta(days=3), feed_kg=Decimal("10"), eggs=100
        )
        feed = make_product("Yem PK-1", min_stock="100")
        warehouse_services.record_stock_transaction(
            actor=cls.warehouse, product_id=feed.pk, transaction_type="IN", quantity=Decimal("150"), document_no="K-1"
        )
        warehouse_services.record_stock_transaction(
            actor=cls.warehouse,
            product_id=feed.pk,
            transaction_type="OUT",
            quantity=Decimal("60"),
            document_no="C-1",
            household=cls.household_a,
        )

    def test_admin_dashboard(self):
        self.login_as(self.warehouse)
        response = self.client.get("/api/dashboard/admin")
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data["households_total"], 2)
        self.assertEqual(data["households_active"], 2)
        self.assertEqual(data["birds_in_households"], 498)
        self.assertEqual(data["birds_in_center"], 500)
        self.assertEqual(data["distributed_30d"], 500)
        self.assertEqual(data["deaths_30d"], 2)
        self.assertEqual((data["deaths_30d_households"], data["deaths_30d_center"]), (2, 0))
        self.assertEqual(data["feed_stock_kg"], 90.0)
        self.assertEqual(data["feed_used_today"], 20.5)
        self.assertEqual(data["eggs_today"], 200)
        self.assertEqual(data["reports_today"], 1)
        self.assertEqual([h["id"] for h in data["households_without_report_today"]], [self.household_b.pk])
        self.assertEqual(data["low_stock"], [{"id": data["low_stock"][0]["id"], "name": "Yem PK-1", "balance": 90.0,
                                             "min_stock": 100.0, "unit": "kg"}])
        self.assertEqual(len(data["recent_movements"]), 3)
        production = data["production_14d"]
        self.assertEqual(len(production), 14)
        self.assertEqual(production[-1], {"date": timezone.localdate().isoformat(), "eggs": 200, "feed_kg": 20.5,
                                          "meat_kg": 0.0})
        self.assertEqual(production[-4]["eggs"], 100)
        self.assertEqual(production[0]["eggs"], 0)
        # TZ §4: total birds, meat, monthly dynamics, warehouse summary.
        self.assertEqual(data["birds_total"], 998)
        self.assertEqual(data["meat_today"], 0.0)
        months = data["production_12m"]
        self.assertEqual(len(months), 12)
        self.assertEqual(months[-1]["month"], timezone.localdate().strftime("%Y-%m"))
        self.assertEqual(sum(month["eggs"] for month in months), sum(day["eggs"] for day in production))
        self.assertEqual(data["warehouse_summary"]["active_products"], 1)
        self.assertEqual(data["warehouse_summary"]["low_stock"], 1)
        self.assertEqual(data["warehouse_summary"]["out_of_stock"], 0)

    def test_owner_dashboard(self):
        self.login_as(self.owner_a)
        response = self.client.get("/api/dashboard/owner")
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data["household"]["id"], self.household_a.pk)
        self.assertEqual(data["bird_count"], 298)
        self.assertEqual(data["today_report"]["eggs"], 200)
        self.assertEqual(data["eggs_7d"], 300)
        self.assertEqual(data["feed_7d"], 30.5)
        self.assertEqual(data["deaths_7d"], 2)
        self.assertEqual(data["deaths_30d"], 2)
        self.assertEqual(data["batches"][0]["count"], 298)
        self.assertEqual(len(data["recent_reports"]), 2)
        self.assertEqual([t["quantity"] for t in data["received_products"]], [60.0])

    def test_permissions(self):
        self.login_as(self.owner_a)
        self.assertEqual(self.client.get("/api/dashboard/admin").status_code, 403)
        self.login_as(self.admin)
        self.assertEqual(self.client.get("/api/dashboard/owner").status_code, 403)

"""No N+1 anywhere: every list / detail / stats endpoint runs the same number of SQL queries on a small
dataset and on a 10× larger one, and stats endpoints stay within the budgets of API_CONTRACT_V2 §7.1
(``force_authenticate`` → the JWT user lookup is not part of the counts).

Set ``NNP_PRINT_QUERY_COUNTS=1`` to print the measured table.
"""

from __future__ import annotations

import os
from datetime import date, timedelta
from decimal import Decimal

from django.db import connection
from django.test import override_settings
from django.test.utils import CaptureQueriesContext

from accounts.models import User
from audit.context import audit_context
from common.testing import BOT_API_TOKEN, make_household
from farm.models import MovementType, PoultryType
from notifications.models import Notification
from warehouse import services as warehouse_services
from warehouse.models import Product, ProductCategory, Unit

from .helpers import FrozenClockTestCase, TODAY, batch, created_on, move, report, staff

#: API_CONTRACT_V2 §7.1
STATS_BUDGET = {
    "stats overview": 8,
    "household stats": 7,
    "my stats": 7,
    "batch stats": 6,
    "product stats": 6,
    "warehouse stats": 6,
    "bot summary owner": 10,
    "bot summary staff": 10,
    "audit logs": 2,
}


@override_settings(BOT_API_TOKEN=BOT_API_TOKEN)
class QueryBudgetTests(FrozenClockTestCase):
    @classmethod
    def setUpTestData(cls):
        cls.admin, cls.warehouse = staff()
        User.objects.filter(pk=cls.warehouse.pk).update(telegram_id=7000)
        cls.households = []
        cls.batches = []
        cls.products = []

    def grow(self, households: int, products: int) -> None:
        """Add households with movements, reports and stock operations (+ audit rows and notifications)."""
        with audit_context("API", "127.0.0.1"):
            breeder = batch(self.admin, households * 100 + 50, date(2026, 9, 1))
            broiler = batch(self.admin, households * 50 + 50, date(2026, 9, 1), PoultryType.BROILER)
            self.batches += [breeder, broiler]
            move(self.admin, breeder, None, MovementType.DEATH, 5, date(2026, 9, 2))
            for _ in range(products):
                index = len(self.products)
                product = Product.objects.create(
                    name=f"Yem {index}", category=ProductCategory.FEED if index % 2 == 0 else ProductCategory.VACCINE,
                    unit=Unit.KG if index % 2 == 0 else Unit.PIECE, min_stock=Decimal("10"),
                )
                warehouse_services.record_stock_transaction(
                    actor=self.warehouse, product_id=product.pk, transaction_type="IN", quantity=Decimal("100000"),
                    document_no="K-1",
                )
                self.products.append(product)
            for _ in range(households):
                index = len(self.households)
                household = make_household(f"Uy {index}", f"budget_owner{index}")
                created_on(household, date(2026, 9, 1))
                User.objects.filter(pk=household.owner_id).update(telegram_id=8000 + index, must_change_password=False)
                move(self.admin, breeder, household, MovementType.DISTRIBUTE, 100, date(2026, 9, 5))
                move(self.admin, broiler, household, MovementType.DISTRIBUTE, 50, date(2026, 9, 5))
                move(self.admin, breeder, household, MovementType.DEATH, 1, date(2026, 10, 20))
                for offset in range(6):
                    report(household, TODAY - timedelta(days=offset * 7), "7.5", "15.5", weight="1900")
                for product in self.products[:3]:
                    warehouse_services.record_stock_transaction(
                        actor=self.warehouse, product_id=product.pk, transaction_type="OUT", quantity=Decimal("3"),
                        document_no="C-1", household=household,
                    )
                Notification.objects.create(
                    recipient=self.warehouse, chat_id=7000, event="test", event_key=f"test:{index}", text="x"
                )
                self.households.append(household)

    def measure(self) -> dict[str, int]:
        first_household = self.households[0]
        owner = User.objects.get(pk=first_household.owner_id)
        first_batch = self.batches[0]
        product = self.products[0]
        plans = [
            (self.admin, "users", "/api/users"),
            (self.admin, "user detail", f"/api/users/{owner.pk}"),
            (self.admin, "households", "/api/households"),
            (self.admin, "household detail", f"/api/households/{first_household.pk}"),
            (self.admin, "households map", "/api/households/map"),
            (self.admin, "household batches", f"/api/households/{first_household.pk}/batches"),
            (self.admin, "household stats", f"/api/households/{first_household.pk}/stats"),
            (self.admin, "batches", "/api/batches"),
            (self.admin, "batch detail", f"/api/batches/{first_batch.pk}"),
            (self.admin, "batch stats", f"/api/batches/{first_batch.pk}/stats"),
            (self.admin, "movements", "/api/movements"),
            (self.admin, "daily reports", "/api/daily-reports"),
            (self.admin, "products", "/api/products"),
            (self.admin, "product detail", f"/api/products/{product.pk}"),
            (self.admin, "product stats", f"/api/products/{product.pk}/stats"),
            (self.admin, "stock transactions", "/api/stock-transactions"),
            (self.admin, "dashboard admin", "/api/dashboard/admin"),
            (self.admin, "stats overview", "/api/stats/overview"),
            (self.admin, "stats overview 1 year", "/api/stats/overview?date_from=2025-11-04"),
            (self.admin, "warehouse stats", "/api/warehouse/stats"),
            (self.admin, "audit logs", "/api/audit-logs"),
            (self.admin, "audit logs filtered", "/api/audit-logs?module=movements&search=TR"),
            (owner, "my stats", "/api/my/stats"),
            (owner, "dashboard owner", "/api/dashboard/owner"),
            (owner, "me", "/api/auth/me"),
            (owner, "owner households", "/api/households"),
            (owner, "owner movements", "/api/movements"),
            (owner, "owner stock transactions", "/api/stock-transactions"),
            (None, "bot user", f"/api/bot/users/{owner.telegram_id}"),
            (None, "bot summary owner", f"/api/bot/users/{owner.telegram_id}/summary"),
            (None, "bot summary staff", "/api/bot/users/7000/summary"),
            (None, "bot pending", "/api/bot/notifications/pending"),
            (None, "health", "/api/health"),
        ]
        counts = {}
        for user, name, url in plans:
            self.client.force_authenticate(user=user)
            headers = {"HTTP_X_BOT_TOKEN": BOT_API_TOKEN} if url.startswith("/api/bot/") else {}
            with CaptureQueriesContext(connection) as ctx:
                response = self.client.get(url, **headers)
            self.assertEqual(response.status_code, 200, (name, response.content[:300]))
            counts[name] = len(ctx.captured_queries)
        return counts

    def test_same_query_count_on_10x_data_and_within_budget(self):
        self.grow(households=3, products=3)
        small = self.measure()
        self.grow(households=27, products=27)
        large = self.measure()
        if os.environ.get("NNP_PRINT_QUERY_COUNTS"):
            print("\nendpoint                      small  10x")
            for name in small:
                print(f"{name:<30}{small[name]:>5}{large[name]:>5}")
        for name in small:
            with self.subTest(endpoint=name):
                self.assertEqual(small[name], large[name], f"{name}: query count grows with data (N+1)")
        for name, budget in STATS_BUDGET.items():
            with self.subTest(budget=name):
                self.assertLessEqual(large[name], budget)

from __future__ import annotations

from decimal import Decimal

from common.testing import BaseAPITestCase, make_product
from notifications.models import Notification

from .models import Product, StockTransaction


class ProductTests(BaseAPITestCase):
    def test_create_and_patch_ignore_balance(self):
        self.login_as(self.warehouse)
        response = self.client.post(
            "/api/products",
            {"name": "Vaksina ND", "category": "VACCINE", "unit": "dona", "min_stock": 50, "balance": 999},
            format="json",
        )
        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.data["balance"], 0)
        self.assertTrue(response.data["is_low"])
        self.assertEqual(response.data["category_display"], "Vaksina")
        pk = response.data["id"]
        response = self.client.patch(f"/api/products/{pk}", {"balance": 5000, "min_stock": 0}, format="json")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["balance"], 0)
        self.assertEqual(response.data["min_stock"], 0)

    def test_duplicate_name_and_low_filter(self):
        make_product("Yem PK-1", min_stock="100")
        self.login_as(self.admin)
        response = self.client.post(
            "/api/products", {"name": "yem pk-1", "category": "FEED", "unit": "kg", "min_stock": 1}, format="json"
        )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(self.client.get("/api/products?low=1").data["count"], 1)


class StockTransactionTests(BaseAPITestCase):
    def setUp(self):
        super().setUp()
        self.product = make_product("Yem PK-1", min_stock="100")
        self.login_as(self.warehouse)

    def post(self, **payload):
        data = {"product": self.product.pk, "document_no": "YX-15", **payload}
        return self.client.post("/api/stock-transactions", data, format="json")

    def test_in_and_out_change_balance(self):
        response = self.post(type="IN", quantity=500)
        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.data["balance_after"], 500)
        self.assertEqual(response.data["op_id"], f"OM-{response.data['id']:06d}")
        response = self.post(type="OUT", quantity="120.5", household=self.household_a.pk)
        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.data["balance_after"], 379.5)
        self.assertEqual(response.data["household"]["id"], self.household_a.pk)
        self.product.refresh_from_db()
        self.assertEqual(self.product.balance, Decimal("379.50"))

    def test_out_more_than_balance_is_rejected(self):
        self.post(type="IN", quantity=50)
        response = self.post(type="OUT", quantity=51)
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.data["quantity"], ["Omborda yetarli emas. Qoldiq: 50 kg"])
        self.product.refresh_from_db()
        self.assertEqual(self.product.balance, Decimal("50"))

    def test_validation(self):
        self.assertEqual(self.post(type="IN", quantity=10, household=self.household_a.pk).status_code, 400)
        self.assertEqual(self.post(type="IN", quantity=0).status_code, 400)
        self.assertEqual(self.post(type="IN", quantity=10, document_no="").status_code, 400)
        Product.objects.filter(pk=self.product.pk).update(is_active=False)
        response = self.post(type="IN", quantity=10)
        self.assertEqual(response.status_code, 400)
        self.assertIn("product", response.data)

    def test_whole_units(self):
        piece = make_product("Shprits", min_stock="0", unit="dona")
        response = self.client.post(
            "/api/stock-transactions",
            {"product": piece.pk, "type": "IN", "quantity": "1.5", "document_no": "D-1"},
            format="json",
        )
        self.assertEqual(response.status_code, 400)

    def test_owner_cannot_post(self):
        self.login_as(self.owner_a)
        self.assertEqual(self.post(type="IN", quantity=10).status_code, 403)

    def test_owner_sees_only_out_to_own_household(self):
        self.post(type="IN", quantity=1000)
        self.post(type="OUT", quantity=10, household=self.household_a.pk)
        self.post(type="OUT", quantity=20, household=self.household_b.pk)
        self.post(type="OUT", quantity=30)
        self.login_as(self.owner_a)
        response = self.client.get("/api/stock-transactions")
        self.assertEqual(response.status_code, 200)
        self.assertEqual([row["quantity"] for row in response.data["results"]], [10])
        response = self.client.get(f"/api/stock-transactions?household={self.household_b.pk}")
        self.assertEqual(response.data["count"], 0)

    def test_low_stock_notification_only_on_crossing(self):
        self.warehouse.telegram_id = 9001
        self.warehouse.save()
        with self.captureOnCommitCallbacks(execute=True):
            self.post(type="IN", quantity=300)
        with self.captureOnCommitCallbacks(execute=True):
            self.post(type="OUT", quantity=150)  # 150 > 100: not low yet
        self.assertEqual(Notification.objects.filter(event="low_stock", chat_id__isnull=False).count(), 0)
        with self.captureOnCommitCallbacks(execute=True):
            self.post(type="OUT", quantity=60)  # 90 <= 100: crossing
        self.assertEqual(Notification.objects.filter(event="low_stock", chat_id__isnull=False).count(), 1)
        with self.captureOnCommitCallbacks(execute=True):
            self.post(type="OUT", quantity=10)  # still low: no new alert
        self.assertEqual(Notification.objects.filter(event="low_stock", chat_id__isnull=False).count(), 1)
        with self.captureOnCommitCallbacks(execute=True):
            self.post(type="IN", quantity=100)  # back above minimum
            self.post(type="OUT", quantity=100)  # crosses again
        self.assertEqual(Notification.objects.filter(event="low_stock", chat_id__isnull=False).count(), 2)
        self.assertEqual(StockTransaction.objects.count(), 6)

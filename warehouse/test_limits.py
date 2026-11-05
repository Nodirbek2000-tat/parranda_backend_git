"""Balance bounds and unit consistency of products."""

from __future__ import annotations

from decimal import Decimal

from common.testing import BaseAPITestCase, make_product

from . import services
from .models import MAX_AMOUNT, Product, Unit


class StockLimitTests(BaseAPITestCase):
    def setUp(self):
        super().setUp()
        self.login_as(self.warehouse)

    def post_in(self, product: Product, quantity: str):
        return self.client.post(
            "/api/stock-transactions",
            {"product": product.pk, "type": "IN", "quantity": quantity, "document_no": "K-1"},
            format="json",
        )

    def test_incoming_beyond_column_capacity_is_400(self):
        product = make_product()
        self.assertEqual(self.post_in(product, str(MAX_AMOUNT)).status_code, 201)
        response = self.post_in(product, "0.01")
        self.assertEqual(response.status_code, 400)
        self.assertIn("quantity", response.data)
        product.refresh_from_db()
        self.assertEqual(product.balance, MAX_AMOUNT)

    def test_unit_cannot_become_whole_with_fractional_balance(self):
        product = make_product(unit=Unit.KG)
        services.record_stock_transaction(
            actor=self.warehouse, product_id=product.pk, transaction_type="IN",
            quantity=Decimal("2.5"), document_no="K-1",
        )
        response = self.client.patch(f"/api/products/{product.pk}", {"unit": "dona"}, format="json")
        self.assertEqual(response.status_code, 400)
        self.assertIn("unit", response.data)
        self.assertEqual(self.client.patch(f"/api/products/{product.pk}", {"unit": "litr"}, format="json").status_code, 200)

    def test_household_is_rejected_for_incoming(self):
        product = make_product()
        response = self.client.post(
            "/api/stock-transactions",
            {"product": product.pk, "type": "IN", "quantity": 5, "document_no": "K-1",
             "household": self.household_a.pk},
            format="json",
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn("household", response.data)

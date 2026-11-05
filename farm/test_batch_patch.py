"""``PATCH /api/batches/{id}`` (API_CONTRACT_V2 §4) and the Phase 2 notification paths (§9)."""

from __future__ import annotations

from decimal import Decimal

from accounts.models import User
from common.testing import BaseAPITestCase, make_product
from notifications.models import Notification
from warehouse import services as warehouse_services

from .models import Household, PoultryBatch

MSG_NOT_EDITABLE = "Bu maydonni o‘zgartirib bo‘lmaydi."


class BatchPatchTests(BaseAPITestCase):
    def patch(self, data, user=None, batch_id=None):
        self.login_as(user or self.admin)
        return self.client.patch(f"/api/batches/{batch_id or self.batch.pk}", data, format="json")

    def test_updates_whitelisted_fields_and_returns_counts(self):
        response = self.patch({"health_status": "  Emlangan  ", "supplier": "Yangi inkubator"}, user=self.warehouse)
        self.assertEqual(response.status_code, 200, response.content)
        data = response.json()
        self.assertEqual((data["health_status"], data["supplier"]), ("Emlangan", "Yangi inkubator"))
        self.assertEqual((data["center_stock"], data["distributed_total"]), (500, 500))
        self.batch.refresh_from_db()
        self.assertEqual(self.batch.health_status, "Emlangan")

    def test_forbidden_and_unknown_keys(self):
        for key, value in (("breed", "X"), ("initial_quantity", 5), ("age_days", 3), ("arrived_at", "2026-01-01"),
                           ("initial_weight_g", 40), ("code", "NNP-1"), ("poultry_type", "BROILER"), ("foo", 1)):
            with self.subTest(key=key):
                response = self.patch({key: value, "supplier": "Y"})
                self.assertEqual((response.status_code, response.json()), (400, {key: [MSG_NOT_EDITABLE]}))
        self.assertEqual(PoultryBatch.objects.get(pk=self.batch.pk).supplier, "Toshkent inkubatori")

    def test_value_rules(self):
        self.assertEqual(
            self.patch({}).json(), {"non_field_errors": ["O‘zgartirish uchun kamida bitta maydon yuboring."]}
        )
        self.assertEqual(self.patch({"supplier": "   "}).json(), {"supplier": ["Bu maydon bo‘sh bo‘lishi mumkin emas."]})
        self.assertEqual(self.patch({"supplier": None}).status_code, 400)
        self.assertEqual(self.patch({"supplier": 123}).json(), {"supplier": ["Matn kiriting."]})
        self.assertEqual(self.patch({"health_status": "x" * 121}).json(),
                         {"health_status": ["Ko‘pi bilan 120 ta belgi kiriting."]})
        self.assertEqual(self.patch({"supplier": "y" * 200}).status_code, 200)
        self.assertEqual(self.patch({"supplier": "y" * 201}).status_code, 400)
        self.assertEqual(self.patch([1, 2]).status_code, 400)

    def test_permissions_and_methods(self):
        self.assertEqual(self.patch({"supplier": "Y"}, user=self.owner_a).status_code, 403)
        self.client.force_authenticate(user=None)
        self.assertEqual(self.client.patch(f"/api/batches/{self.batch.pk}", {"supplier": "Y"}, format="json").status_code,
                         401)
        self.login_as(self.admin)
        self.assertEqual(self.client.put(f"/api/batches/{self.batch.pk}", {"supplier": "Y"}, format="json").status_code,
                         405)
        self.assertEqual(self.client.delete(f"/api/batches/{self.batch.pk}").status_code, 405)
        self.assertEqual(self.patch({"supplier": "Y"}, batch_id=999999).status_code, 404)

    def test_patch_sends_no_notification(self):
        User.objects.filter(pk=self.admin.pk).update(telegram_id=1001)
        with self.captureOnCommitCallbacks(execute=True):
            self.patch({"supplier": "Y"})
        self.assertEqual(Notification.objects.count(), 0)


class NotificationPathTests(BaseAPITestCase):
    """Phase 2 changes button paths to the new detail pages (owners keep ``/my``)."""

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        User.objects.filter(pk=cls.admin.pk).update(telegram_id=1001)
        User.objects.filter(pk=cls.warehouse.pk).update(telegram_id=1002)
        User.objects.filter(pk=cls.owner_a.pk).update(telegram_id=1003)

    def paths(self, event: str) -> set[tuple[int, str]]:
        return {(n.recipient_id, n.button_path) for n in Notification.objects.filter(event=event)}

    def test_batch_out_and_center_death(self):
        self.login_as(self.admin)
        with self.captureOnCommitCallbacks(execute=True):
            created = self.client.post(
                "/api/batches",
                {"poultry_type": "LAYER", "breed": "X", "initial_quantity": 10, "age_days": 1, "supplier": "S",
                 "arrived_at": "2026-01-01"},
                format="json",
            ).json()
        path = f"/batches/{created['id']}"
        self.assertEqual(self.paths("batch_created"), {(self.admin.pk, path), (self.warehouse.pk, path)})
        with self.captureOnCommitCallbacks(execute=True):
            self.client.post("/api/movements", {"batch": self.batch.pk, "type": "OUT", "quantity": 1}, format="json")
            self.client.post("/api/movements", {"batch": self.batch.pk, "type": "DEATH", "quantity": 1}, format="json")
        batch_path = f"/batches/{self.batch.pk}"
        self.assertEqual(self.paths("poultry_out"), {(self.admin.pk, batch_path)})
        self.assertEqual(self.paths("poultry_death"), {(self.admin.pk, batch_path)})

    def test_stock_paths(self):
        product = make_product(min_stock="50")
        with self.captureOnCommitCallbacks(execute=True):
            warehouse_services.record_stock_transaction(
                actor=self.warehouse, product_id=product.pk, transaction_type="IN", quantity=Decimal("100"),
                document_no="K-1",
            )
            warehouse_services.record_stock_transaction(
                actor=self.warehouse, product_id=product.pk, transaction_type="OUT", quantity=Decimal("60"),
                document_no="C-1", household=Household.objects.select_related("owner").get(pk=self.household_a.pk),
            )
        path = f"/warehouse/{product.pk}"
        self.assertEqual(self.paths("stock_in"), {(self.warehouse.pk, path)})
        self.assertEqual(self.paths("stock_out"), {(self.warehouse.pk, path), (self.owner_a.pk, "/my")})
        self.assertEqual(self.paths("low_stock"), {(self.warehouse.pk, path), (self.admin.pk, path)})

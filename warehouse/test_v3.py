"""Warehouse V3: lots + expiry, FEFO consumption, inventory count, check_integrity (API_CONTRACT_V3 §5, §19.1)."""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal
from io import StringIO

from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from accounts.models import User
from audit.models import AuditLog
from common.roles import Role
from common.testing import BaseAPITestCase, make_product, make_user
from notifications.models import Notification

from . import services
from .models import (
    InventoryCount,
    InventoryCountStatus,
    Product,
    StockLot,
    StockLotAllocation,
    StockTransaction,
    StockTransactionType,
    Unit,
)
from .reminders import lot_expired_job, lot_expiring_job


class WarehouseV3Base(BaseAPITestCase):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.vet = make_user("vet1", Role.VET)
        cls.logistics = make_user("log1", Role.LOGISTICS)

    def setUp(self):
        super().setUp()
        self.product = make_product("Yem PK-1", min_stock="10")
        self.today = timezone.localdate()

    def post_tx(self, **payload):
        data = {"product": self.product.pk, "document_no": "YX-1", **payload}
        return self.client.post("/api/stock-transactions", data, format="json")

    def receive(self, quantity, *, lot_no="", expiry=None) -> StockTransaction:
        return services.record_stock_transaction(
            actor=self.warehouse,
            product_id=self.product.pk,
            transaction_type=StockTransactionType.IN,
            quantity=Decimal(quantity),
            document_no="YX-IN",
            lot_no=lot_no,
            expiry_date=expiry,
        )


class LotTests(WarehouseV3Base):
    def test_in_creates_one_lot_and_is_listed(self):
        self.login_as(self.warehouse)
        expiry = self.today + timedelta(days=10)
        response = self.post_tx(type="IN", quantity=500, lot_no="  L-11 ", expiry_date=expiry.isoformat())
        self.assertEqual(response.status_code, 201, response.data)
        lot = StockLot.objects.get()
        self.assertEqual((lot.lot_no, lot.received_qty, lot.remaining_qty), ("L-11", Decimal("500"), Decimal("500")))
        self.assertEqual(response.data["lots"], [
            {"lot_id": lot.pk, "lot_no": "L-11", "expiry_date": expiry.isoformat(), "quantity": 500.0}
        ])
        self.assertIsNone(response.data["reference"])
        listed = self.client.get("/api/stock-lots").data["results"][0]
        self.assertEqual(listed["status"], "EXPIRING")
        self.assertEqual(listed["status_display"], "Muddati yaqin")
        self.assertEqual(listed["days_to_expiry"], 10)
        self.assertEqual(listed["in_transaction"]["op_id"], f"OM-{response.data['id']:06d}")
        product = self.client.get(f"/api/products/{self.product.pk}").data
        self.assertEqual(product["nearest_expiry"], expiry.isoformat())
        self.assertEqual((product["reserved"], product["available"]), (0, 500))

    def test_expired_in_and_lot_on_wrong_type_rejected(self):
        self.login_as(self.warehouse)
        response = self.post_tx(type="IN", quantity=5, expiry_date=(self.today - timedelta(days=1)).isoformat())
        self.assertEqual(response.data["expiry_date"], ["Yaroqlilik muddati o‘tgan mahsulotni kirim qilib bo‘lmaydi."])
        self.receive(10)
        response = self.post_tx(type="OUT", quantity=1, lot_no="L-1")
        self.assertEqual(response.data["lot_no"], ["Partiya faqat kirimda ko‘rsatiladi."])
        for tx_type in ("ADJUST_IN", "ADJUST_OUT"):
            response = self.post_tx(type=tx_type, quantity=1)
            self.assertEqual(response.status_code, 400)
            self.assertEqual(response.data["type"], ["Bu turdagi operatsiya faqat inventarizatsiya orqali yaratiladi."])
        self.assertEqual(StockTransaction.objects.count(), 1)

    def test_out_consumes_untracked_first_then_fefo(self):
        # 30 kg existed before lots (untracked): create it as an ADJUST_IN through an approved count.
        Product.objects.filter(pk=self.product.pk).update(balance=Decimal("0"))
        late = self.receive(40, lot_no="LATE", expiry=self.today + timedelta(days=90))
        none = self.receive(20, lot_no="NONE")
        soon = self.receive(50, lot_no="SOON", expiry=self.today + timedelta(days=5))
        self.product.refresh_from_db()
        with self.captureOnCommitCallbacks(execute=True):
            services.apply_adjustment(
                actor=self.admin, product=Product.objects.select_for_update().get(pk=self.product.pk),
                difference=Decimal("30"), document_no="INV-000001",
            )
        self.login_as(self.warehouse)
        response = self.post_tx(type="OUT", quantity=100, household=self.household_a.pk)
        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(
            [(item["lot_no"], item["quantity"]) for item in response.data["lots"]], [("SOON", 50.0), ("LATE", 20.0)]
        )
        remaining = {lot.lot_no: lot.remaining_qty for lot in StockLot.objects.all()}
        self.assertEqual(remaining, {"LATE": Decimal("20"), "NONE": Decimal("20"), "SOON": Decimal("0")})
        self.assertEqual(StockLotAllocation.objects.count(), 2)
        self.assertTrue(late and none and soon)

    def test_explicit_lot_consumed_first_and_validated(self):
        first = self.receive(10, lot_no="A", expiry=self.today + timedelta(days=3))
        second = self.receive(10, lot_no="B", expiry=self.today + timedelta(days=30))
        self.login_as(self.warehouse)
        response = self.post_tx(type="OUT", quantity=11, lot=second.lot.pk)
        self.assertEqual(response.data["lot"], ["Tanlangan partiyada yetarli qoldiq yo‘q."])
        response = self.post_tx(type="OUT", quantity=4, lot=second.lot.pk)
        self.assertEqual(response.status_code, 201)
        self.assertEqual([item["lot_no"] for item in response.data["lots"]], ["B"])
        first.lot.refresh_from_db()
        self.assertEqual(first.lot.remaining_qty, Decimal("10"))

    def test_lot_filters_statuses_and_permissions(self):
        self.receive(5, lot_no="OK", expiry=self.today + timedelta(days=200))
        self.receive(5, lot_no="SOON", expiry=self.today + timedelta(days=2))
        expired = self.receive(5, lot_no="OLD", expiry=self.today + timedelta(days=1))
        StockLot.objects.filter(pk=expired.lot.pk).update(expiry_date=self.today - timedelta(days=3))
        self.receive(5, lot_no="NOEXP")
        self.login_as(self.vet)
        rows = self.client.get("/api/stock-lots").data["results"]
        self.assertEqual([row["lot_no"] for row in rows], ["OLD", "SOON", "OK", "NOEXP"])
        self.assertEqual([row["status"] for row in rows], ["EXPIRED", "EXPIRING", "OK", "OK"])
        within = self.client.get("/api/stock-lots?expiring_within=7").data["results"]
        self.assertEqual([row["lot_no"] for row in within], ["OLD", "SOON"])
        self.assertEqual([row["lot_no"] for row in self.client.get("/api/stock-lots?expired=1").data["results"]], ["OLD"])
        self.assertEqual(self.client.get("/api/stock-lots?expiring_within=400").status_code, 400)
        product_lots = self.client.get(f"/api/products/{self.product.pk}/lots").data
        self.assertEqual(product_lots["count"], 4)
        self.assertEqual(self.client.get("/api/products").status_code, 200)
        self.assertEqual(self.client.post("/api/products", {"name": "X"}, format="json").status_code, 403)
        self.assertEqual(self.client.get("/api/inventory-counts").status_code, 403)
        self.login_as(self.logistics)
        self.assertEqual(self.client.get("/api/stock-lots").status_code, 403)
        self.assertEqual(self.client.get("/api/products").status_code, 403)
        self.login_as(self.owner_a)
        self.assertEqual(self.client.get("/api/stock-lots").status_code, 403)

    def test_lot_list_query_budget(self):
        for index in range(12):
            self.receive(3, lot_no=f"L-{index}", expiry=self.today + timedelta(days=index))
        self.login_as(self.warehouse)
        self.client.get("/api/stock-lots")  # warm up readiness cache
        with CaptureQueriesContext(connection) as ctx:
            response = self.client.get("/api/stock-lots")
        self.assertEqual(response.data["count"], 12)
        self.assertLessEqual(len(ctx.captured_queries), 6)

    def test_reminder_jobs_are_idempotent(self):
        self.receive(5, lot_no="SOON", expiry=self.today + timedelta(days=3))
        old = self.receive(5, lot_no="OLD", expiry=self.today + timedelta(days=1))
        StockLot.objects.filter(pk=old.lot.pk).update(expiry_date=self.today - timedelta(days=1))
        queued = []
        for _ in range(2):
            with self.captureOnCommitCallbacks(execute=True):
                queued.append(lot_expiring_job(self.today) + lot_expired_job(self.today))
        soon_rows = Notification.objects.filter(event="lot_expiring")
        # “queued” counts new rows only: the same-day re-run reports 0 (§16.1).
        self.assertEqual(queued, [Notification.objects.filter(event__in=("lot_expiring", "lot_expired")).count(), 0])
        self.assertGreater(queued[0], 0)
        expired_rows = Notification.objects.filter(event="lot_expired")
        self.assertTrue(soon_rows.exists())
        self.assertTrue(expired_rows.exists())
        self.assertEqual(soon_rows.count(), soon_rows.values("recipient_id").distinct().count())
        self.assertTrue(all(":%d.soon:" % StockLot.objects.get(lot_no="SOON").pk in row.event_key for row in soon_rows))


class InventoryTests(WarehouseV3Base):
    def setUp(self):
        super().setUp()
        self.vaccine = Product.objects.create(name="Vaksina ND", category="VACCINE", unit=Unit.PIECE, min_stock=0)
        self.receive(100, lot_no="Y1", expiry=self.today + timedelta(days=60))
        services.record_stock_transaction(
            actor=self.warehouse, product_id=self.vaccine.pk, transaction_type="IN", quantity=Decimal("50"),
            document_no="V-1",
        )

    def create(self, **payload):
        return self.client.post("/api/inventory-counts", payload, format="json")

    def lines(self, pk, lines):
        return self.client.patch(f"/api/inventory-counts/{pk}/lines", {"lines": lines}, format="json")

    def test_full_flow_with_live_balance_and_adjustments(self):
        self.login_as(self.warehouse)
        response = self.create(note="Oy oxiri")
        self.assertEqual(response.status_code, 201, response.data)
        count = response.data
        self.assertEqual(count["code"], f"INV-{count['id']:06d}")
        self.assertEqual(count["status"], "DRAFT")
        self.assertEqual(len(count["lines"]), 2)
        self.assertEqual(count["allowed_actions"], ["count", "cancel"])
        pk = count["id"]
        # Stock moves after the snapshot: approval must use the live balance (100 − 30 = 70).
        services.record_stock_transaction(
            actor=self.warehouse, product_id=self.product.pk, transaction_type="OUT", quantity=Decimal("30"),
            document_no="YX-2",
        )
        response = self.lines(pk, [{"product": self.product.pk, "counted_qty": 65},
                                   {"product": self.vaccine.pk, "counted_qty": 52}])
        self.assertEqual(response.status_code, 200, response.data)
        feed_line = next(line for line in response.data["lines"] if line["product"]["id"] == self.product.pk)
        self.assertEqual((feed_line["expected_qty"], feed_line["current_balance"], feed_line["counted_qty"]),
                         (100, 70, 65))
        listed = self.client.get("/api/inventory-counts").data["results"][0]
        self.assertEqual((listed["lines_total"], listed["lines_counted"], listed["lines_with_difference"]), (2, 2, 2))
        self.assertEqual(self.client.post(f"/api/inventory-counts/{pk}/approve").status_code, 403)
        self.login_as(self.admin)
        self.assertEqual(self.client.get(f"/api/inventory-counts/{pk}").data["allowed_actions"],
                         ["count", "approve", "cancel"])
        with self.captureOnCommitCallbacks(execute=True):
            response = self.client.post(f"/api/inventory-counts/{pk}/approve")
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["status"], "APPROVED")
        lines = {line["product"]["id"]: line for line in response.data["lines"]}
        self.assertEqual((lines[self.product.pk]["balance_at_approval"], lines[self.product.pk]["difference"]), (70, -5))
        self.assertEqual(lines[self.product.pk]["adjustment"]["type"], "ADJUST_OUT")
        self.assertEqual(lines[self.vaccine.pk]["adjustment"]["type"], "ADJUST_IN")
        out = StockTransaction.objects.get(type=StockTransactionType.ADJUST_OUT)
        self.assertEqual((out.quantity, out.balance_after, out.document_no), (Decimal("5"), Decimal("65"), count["code"]))
        self.assertIsNone(out.household_id)
        self.assertEqual(out.lot_allocations.get().quantity, Decimal("5"))  # lots: 70 tracked, 0 untracked
        adj_in = StockTransaction.objects.get(type=StockTransactionType.ADJUST_IN)
        self.assertEqual(adj_in.balance_after, Decimal("52"))
        self.assertFalse(StockLot.objects.filter(in_transaction=adj_in).exists())  # ADJUST_IN stays untracked
        self.product.refresh_from_db()
        self.assertEqual(self.product.balance, Decimal("65"))
        self.assertTrue(Notification.objects.filter(event="inventory_approved").exists())
        self.assertEqual(AuditLog.objects.filter(module="inventory", action="approve").count(), 1)
        self.assertEqual(AuditLog.objects.filter(module="stock", object_type="StockTransaction",
                                                 after__type="ADJUST_OUT").count(), 1)
        # Double submit → 409, nothing changes.
        response = self.client.post(f"/api/inventory-counts/{pk}/approve")
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.data["detail"], "Inventarizatsiya holati: Tasdiqlangan. Amalni bajarib bo‘lmaydi.")
        self.assertEqual(StockTransaction.objects.filter(type__startswith="ADJUST").count(), 2)
        self.assertEqual(self.lines(pk, [{"product": self.product.pk, "counted_qty": 1}]).status_code, 409)
        # Stats: closing balance includes adjustments.
        stats = self.client.get(f"/api/products/{self.product.pk}/stats").data
        self.assertEqual(stats["totals"]["closing_balance"], 65)
        self.assertEqual(stats["totals"]["out_qty"], 30)
        self.assertEqual(stats["totals"]["adjust_out_qty"], 5)
        out_buf = StringIO()
        call_command("check_integrity", stdout=out_buf)
        self.assertIn("Nomuvofiqlik topilmadi", out_buf.getvalue())

    def test_validation_rules(self):
        self.login_as(self.warehouse)
        self.assertEqual(self.create(category="EQUIPMENT").data["non_field_errors"],
                         ["Inventarizatsiya uchun mahsulot topilmadi."])
        pk = self.create(category="VACCINE").data["id"]
        self.assertEqual(InventoryCount.objects.get(pk=pk).lines.count(), 1)
        response = self.lines(pk, [{"product": self.product.pk, "counted_qty": 1}])
        self.assertEqual(response.status_code, 400)
        response = self.lines(pk, [{"product": self.vaccine.pk, "counted_qty": "1.5"}])
        self.assertIn("butun son", response.data["lines"][0])
        self.login_as(self.admin)
        response = self.client.post(f"/api/inventory-counts/{pk}/approve")
        self.assertEqual(response.data["lines"], ["1 ta mahsulot sanalmagan."])
        response = self.client.post(f"/api/inventory-counts/{pk}/cancel", {"reason": "ab"}, format="json")
        self.assertEqual(response.status_code, 400)
        response = self.client.post(f"/api/inventory-counts/{pk}/cancel", {"reason": "Xato yaratildi"}, format="json")
        self.assertEqual(response.data["status"], "CANCELLED")
        self.assertEqual(self.client.post(f"/api/inventory-counts/{pk}/cancel", {"reason": "Yana"}, format="json").status_code, 409)
        self.assertEqual(self.client.get("/api/inventory-counts?status=CANCELLED").data["count"], 1)
        self.assertEqual(self.client.get("/api/inventory-counts/999999").status_code, 404)

    def test_counted_below_reserved_rolls_back(self):
        Product.objects.filter(pk=self.product.pk).update(reserved=Decimal("40"))
        self.login_as(self.admin)
        pk = self.create(product_ids=[self.product.pk]).data["id"]
        self.lines(pk, [{"product": self.product.pk, "counted_qty": 30}])
        response = self.client.post(f"/api/inventory-counts/{pk}/approve")
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.data["lines"], ["“Yem PK-1”: sanalgan miqdor zaxiralangan miqdordan (40 kg) kam."])
        self.assertFalse(StockTransaction.objects.filter(type__startswith="ADJUST").exists())
        self.assertEqual(InventoryCount.objects.get(pk=pk).status, InventoryCountStatus.DRAFT)

    def test_inventory_query_budgets(self):
        self.login_as(self.admin)
        for _ in range(3):
            self.create()
        self.client.get("/api/inventory-counts")
        with CaptureQueriesContext(connection) as ctx:
            response = self.client.get("/api/inventory-counts")
        self.assertEqual(response.data["count"], 3)
        self.assertLessEqual(len(ctx.captured_queries), 6)
        pk = response.data["results"][0]["id"]
        with CaptureQueriesContext(connection) as ctx:
            self.client.get(f"/api/inventory-counts/{pk}")
        self.assertLessEqual(len(ctx.captured_queries), 10)

    def test_role_matrix(self):
        for user, expected in ((self.admin, 200), (self.warehouse, 200), (self.vet, 403), (self.logistics, 403),
                               (self.owner_a, 403)):
            self.login_as(user)
            self.assertEqual(self.client.get("/api/inventory-counts").status_code, expected, user.username)


class IntegrityTests(WarehouseV3Base):
    def test_lot_invariants_detected(self):
        row = self.receive(10, lot_no="L")
        call_command("check_integrity", stdout=StringIO())
        StockLot.objects.filter(pk=row.lot.pk).update(remaining_qty=Decimal("7"))
        out = StringIO()
        with self.assertRaises(CommandError):
            call_command("check_integrity", stdout=out)
        self.assertIn("partiya", out.getvalue().lower())


class PostLockedTests(WarehouseV3Base):
    def test_post_locked_out_and_return_in_with_reference(self):
        self.receive(20)
        with self.captureOnCommitCallbacks(execute=True):
            product = Product.objects.select_for_update().get(pk=self.product.pk)
            row = services.post_locked(
                actor=self.admin, product=product, transaction_type="OUT", quantity=Decimal("5"),
                document_no="LG-000001", household=self.household_a, notify_owner=False,
                reference=("logistics_task", 1),
            )
        self.assertEqual((row.reference_type, row.reference_id, row.balance_after), ("logistics_task", 1, Decimal("15")))
        stock_out = Notification.objects.filter(event="stock_out")
        self.assertFalse(stock_out.filter(recipient=self.owner_a).exists())
        self.login_as(self.admin)
        listed = self.client.get("/api/stock-transactions?type=OUT").data["results"][0]
        self.assertEqual(listed["reference"], {"type": "logistics_task", "id": 1, "code": "LG-000001",
                                               "path": "/logistics/1"})
        self.login_as(self.owner_a)
        listed = self.client.get("/api/stock-transactions").data["results"][0]
        self.assertEqual(listed["reference"]["path"], "/my/deliveries")


class VerifierFindingsTests(WarehouseV3Base):
    """Independent verification (V3 §5.2–§5.4, §10.2, §13, §17): regressions of the bugs found in review."""

    def setUp(self):
        super().setUp()
        self.receive(100, lot_no="Y1", expiry=self.today + timedelta(days=60))

    def _approved_count(self, counted) -> int:
        self.login_as(self.admin)
        pk = self.client.post("/api/inventory-counts", {"product_ids": [self.product.pk]}, format="json").data["id"]
        self.client.patch(f"/api/inventory-counts/{pk}/lines",
                          {"lines": [{"product": self.product.pk, "counted_qty": counted}]}, format="json")
        with self.captureOnCommitCallbacks(execute=True):
            response = self.client.post(f"/api/inventory-counts/{pk}/approve")
        self.assertEqual(response.status_code, 200, response.data)
        return pk

    def test_adjustment_carries_inventory_reference(self):
        pk = self._approved_count(90)
        row = self.client.get("/api/stock-transactions?type=ADJUST_OUT").data["results"][0]
        self.assertEqual(row["reference"], {"type": "inventory", "id": pk, "code": f"INV-{pk:06d}",
                                            "path": f"/warehouse/inventory/{pk}"})
        self.assertEqual(row["document_no"], f"INV-{pk:06d}")
        self.assertEqual(row["lots"], [{"lot_id": StockLot.objects.get().pk, "lot_no": "Y1",
                                        "expiry_date": (self.today + timedelta(days=60)).isoformat(),
                                        "quantity": 10.0}])
        pk2 = self._approved_count(95)
        row = self.client.get("/api/stock-transactions?type=ADJUST_IN").data["results"][0]
        self.assertEqual(row["reference"]["path"], f"/warehouse/inventory/{pk2}")
        self.assertEqual(row["lots"], [])

    def test_adjust_out_crossing_min_stock_notifies_low_stock(self):
        Product.objects.filter(pk=self.product.pk).update(min_stock=Decimal("50"))
        self._approved_count(40)
        self.assertTrue(Notification.objects.filter(event="low_stock").exists())
        self.assertFalse(Notification.objects.filter(event="stock_out").exists())  # adjustments are not OUTs

    def test_expiry_on_non_in_uses_lot_no_key(self):
        self.login_as(self.warehouse)
        response = self.post_tx(type="OUT", quantity=1, expiry_date=(self.today + timedelta(days=5)).isoformat())
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.data["lot_no"], ["Partiya faqat kirimda ko‘rsatiladi."])

    def test_out_beyond_available_names_reserved_part(self):
        Product.objects.filter(pk=self.product.pk).update(reserved=Decimal("70"))
        self.login_as(self.warehouse)
        response = self.post_tx(type="OUT", quantity=31)
        self.assertEqual(response.data["quantity"], [
            "Omborda yetarli emas. Bo‘sh qoldiq: 30 kg (70 kg yetkazib berish uchun zaxiralangan)"
        ])
        self.assertEqual(self.post_tx(type="OUT", quantity=30).status_code, 201)

    def test_lot_status_boundaries_follow_settings(self):
        from core.settings_service import get_settings

        soon = int(get_settings().expiry_reminder_days)
        self.receive(5, lot_no="EDGE", expiry=self.today + timedelta(days=soon))
        self.receive(5, lot_no="AFTER", expiry=self.today + timedelta(days=soon + 1))
        self.receive(5, lot_no="TODAY", expiry=self.today)
        self.login_as(self.warehouse)
        rows = {row["lot_no"]: row for row in self.client.get("/api/stock-lots?has_remaining=1").data["results"]}
        self.assertEqual(rows["TODAY"]["status"], "EXPIRING")
        self.assertEqual(rows["TODAY"]["days_to_expiry"], 0)
        self.assertEqual(rows["EDGE"]["status"], "EXPIRING")
        self.assertEqual(rows["AFTER"]["status"], "OK")
        within = [row["lot_no"] for row in self.client.get(f"/api/stock-lots?expiring_within={soon}").data["results"]]
        self.assertEqual(within, ["TODAY", "EDGE"])

    def test_inventory_write_roles_and_audit_rows(self):
        self.login_as(self.vet)
        self.assertEqual(self.client.post("/api/inventory-counts", {}, format="json").status_code, 403)
        self.assertEqual(self.client.post("/api/stock-transactions",
                                          {"product": self.product.pk, "type": "IN", "quantity": 1,
                                           "document_no": "X"}, format="json").status_code, 403)
        self.login_as(self.warehouse)
        before = AuditLog.objects.filter(module="inventory").count()
        pk = self.client.post("/api/inventory-counts", {}, format="json").data["id"]
        self.assertEqual(AuditLog.objects.filter(module="inventory", action="create").count(), 1)
        # A rejected PATCH writes nothing; an unchanged value writes nothing.
        bad = self.client.patch(f"/api/inventory-counts/{pk}/lines",
                                {"lines": [{"product": self.product.pk, "counted_qty": -1}]}, format="json")
        self.assertEqual(bad.status_code, 400)
        line = {"lines": [{"product": self.product.pk, "counted_qty": 99}]}
        self.assertEqual(self.client.patch(f"/api/inventory-counts/{pk}/lines", line, format="json").status_code, 200)
        self.client.patch(f"/api/inventory-counts/{pk}/lines", line, format="json")
        self.assertEqual(AuditLog.objects.filter(module="inventory", action="update").count(), 1)
        response = self.client.post(f"/api/inventory-counts/{pk}/cancel", {"reason": "Qayta sanaladi"}, format="json")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(AuditLog.objects.filter(module="inventory").count(), before + 3)
        self.assertEqual(StockTransaction.objects.filter(type__startswith="ADJUST").count(), 0)
        for user in (self.logistics, self.owner_a):
            self.login_as(user)
            self.assertEqual(self.client.get(f"/api/inventory-counts/{pk}").status_code, 403)

    def test_failed_approval_writes_no_audit_and_no_stock(self):
        Product.objects.filter(pk=self.product.pk).update(reserved=Decimal("50"))
        self.login_as(self.admin)
        pk = self.client.post("/api/inventory-counts", {"product_ids": [self.product.pk]}, format="json").data["id"]
        self.client.patch(f"/api/inventory-counts/{pk}/lines",
                          {"lines": [{"product": self.product.pk, "counted_qty": 10}]}, format="json")
        audit_before = AuditLog.objects.count()
        with self.captureOnCommitCallbacks(execute=True):
            self.assertEqual(self.client.post(f"/api/inventory-counts/{pk}/approve").status_code, 400)
        self.assertEqual(AuditLog.objects.count(), audit_before)
        self.assertFalse(Notification.objects.filter(event="inventory_approved").exists())
        self.product.refresh_from_db()
        self.assertEqual(self.product.balance, Decimal("100"))

    def test_owner_dashboard_never_links_staff_pages(self):
        with self.captureOnCommitCallbacks(execute=True):
            product = Product.objects.select_for_update().get(pk=self.product.pk)
            services.post_locked(
                actor=self.admin, product=product, transaction_type="OUT", quantity=Decimal("5"),
                document_no="LG-000077", household=self.household_a, notify_owner=False,
                reference=("logistics_task", 77),
            )
        self.login_as(self.owner_a)
        received = self.client.get("/api/dashboard/owner").data["received_products"]
        self.assertEqual(len(received), 1)
        self.assertEqual(received[0]["reference"]["path"], "/my/deliveries")
        # V3 §5.5: no central balance, lot data or staff names for owners.
        self.assertEqual(received[0]["lots"], [])
        self.assertIsNone(received[0]["balance_after"])
        self.assertEqual(received[0]["created_by"], {"id": None, "full_name": "NNP"})
        rows = self.client.get("/api/stock-transactions").data["results"]
        self.assertEqual([(row["balance_after"], row["lots"], row["created_by"]["id"]) for row in rows],
                         [(None, [], None)])
        self.login_as(self.warehouse)
        staff_row = self.client.get("/api/stock-transactions").data["results"][0]
        self.assertEqual(staff_row["lots"][0]["lot_no"], "Y1")
        self.assertIsNotNone(staff_row["balance_after"])

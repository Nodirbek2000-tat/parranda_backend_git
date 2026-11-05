"""Independent verification of the orders module against API_CONTRACT_V3 §6 / §9.4 / §13 / §17 / §19.

Runs the real logistics services end to end (order → approve + task → assign → accept → start → deliver →
confirm) and checks what the orders side guarantees: order status sync, stock OUT exactly once, history,
audit rows, notifications and the owner's view. Also covers input validation messages and ``409`` replays
that the main suite does not.
"""

from __future__ import annotations

import shutil
import tempfile
import threading
from decimal import Decimal

from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import OperationalError, connection, connections
from django.test import TransactionTestCase, override_settings
from django.test.utils import CaptureQueriesContext

from audit.models import AuditLog
from common.roles import Role
from common.testing import make_household, make_product, make_user
from notifications.models import Notification
from orders import bridge, services
from orders.models import Order, OrderStatus, OrderStatusLog
from warehouse.models import Product, StockTransaction, StockTransactionType

from .test_orders import URL, OrdersBase, no_logistics

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64


def order_audits(order: Order):
    return AuditLog.objects.filter(module="orders", object_type="Order", object_id=order.pk)


class OrderInputVerifyTests(OrdersBase):
    def test_approve_body_validation_is_uzbek_and_changes_nothing(self):
        order = self.make_order()
        self.login_as(self.admin)
        cases = [
            ({"items": [], "create_task": False}, "items"),
            ({"create_task": False}, "items"),
            ({"items": [{"id": order.items.get().pk, "approved_qty": -1}], "create_task": False}, "items"),
            ({"items": [{"id": 999999, "approved_qty": 5}], "create_task": False}, "items"),
        ]
        for body, field in cases:
            with self.subTest(body=body):
                response = self.client.post(f"{URL}/{order.pk}/approve", body, format="json")
                self.assertEqual(response.status_code, 400, response.data)
                self.assertIn(field, response.data)
                message = str(response.data[field][0] if isinstance(response.data[field], list) else response.data[field])
                self.assertNotRegex(message, r"(?i)this (list|field)|may not|required\.$")
        response = self.client.post(f"{URL}/{order.pk}/approve", {"items": [], "create_task": False}, format="json")
        self.assertEqual(response.data, {"items": ["Ro‘yxat bo‘sh bo‘lishi mumkin emas."]})
        other = self.make_order(self.owner_b)
        self.login_as(self.admin)
        response = self.client.post(f"{URL}/{order.pk}/approve",
                                    {"items": [{"id": other.items.get().pk, "approved_qty": 5}], "create_task": False},
                                    format="json")
        self.assertEqual(response.data, {"items": [services.MSG_APPROVE_ALL_ITEMS]})
        order.refresh_from_db()
        self.assertEqual(order.status, OrderStatus.NEW)
        self.assertFalse(order_audits(order).filter(action="status_change").exists())

    def test_reject_rules(self):
        order = self.make_order()
        for user in (self.owner_a, self.warehouse, self.logist, self.vet):
            self.login_as(user)
            with self.subTest(user=user.username):
                response = self.client.post(f"{URL}/{order.pk}/reject", {"reason": "Ombor bo‘sh"}, format="json")
                self.assertEqual(response.status_code, 403)
        self.login_as(self.admin)
        response = self.client.post(f"{URL}/{order.pk}/reject", {"reason": "x" * 501}, format="json")
        self.assertEqual(response.status_code, 400)
        response = self.client.post(f"{URL}/{order.pk}/reject", {"reason": "ab"}, format="json")
        self.assertEqual(response.data, {"reason": ["Sababni yozing (kamida 3 ta belgi)."]})
        self.assertEqual(self.client.post(f"{URL}/999999/reject", {"reason": "abc"}, format="json").status_code, 404)

        with self.captureOnCommitCallbacks(execute=True):
            response = self.client.post(f"{URL}/{order.pk}/reject", {"reason": "Ombor bo‘sh"}, format="json")
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["allowed_actions"], [])
        row = Notification.objects.get(event="order_rejected")
        self.assertEqual((row.recipient_id, row.button_path), (self.owner_a.pk, f"/my/orders/{order.pk}"))
        self.assertIn("Sabab: Ombor bo‘sh", row.text)
        self.assertIn("BUYURTMA RAD ETILDI", row.text)

        # Replays: 409, nothing new is written.
        audits = order_audits(order).count()
        for action, body in (("reject", {"reason": "yana"}), ("approve", {"items": [], "create_task": False}),
                             ("cancel", {"reason": "kerak emas"})):
            with self.subTest(action=action):
                response = self.client.post(f"{URL}/{order.pk}/{action}", body, format="json")
                self.assertIn(response.status_code, (400, 409))
        response = self.client.post(f"{URL}/{order.pk}/cancel", {"reason": "kerak emas"}, format="json")
        self.assertEqual(response.data["detail"], "Buyurtma holati: Rad etildi. Amalni bajarib bo‘lmaydi.")
        self.assertEqual(order_audits(order).count(), audits)
        self.assertEqual(OrderStatusLog.objects.filter(order=order).count(), 2)

    def test_double_approve_through_the_service_is_409(self):
        order = self.make_order()
        item = order.items.get()
        rows = [{"id": item.pk, "approved_qty": Decimal("10")}]
        services.approve_order(actor=self.admin, order_id=order.pk, items=rows, create_task=False)
        with self.assertRaises(services.Conflict):
            services.approve_order(actor=self.admin, order_id=order.pk, items=rows, create_task=False)
        self.assertEqual(OrderStatusLog.objects.filter(order=order, to_status=OrderStatus.APPROVED).count(), 1)
        # Over HTTP the replay is a 409 as well and writes no second audit row.
        self.login_as(self.admin)
        body = {"items": [{"id": item.pk, "approved_qty": 10}], "create_task": False}
        self.assertEqual(self.client.post(f"{URL}/{order.pk}/approve", body, format="json").status_code, 409)
        self.assertEqual(order_audits(order).filter(action="status_change").count(), 0)

    def test_owner_cannot_use_admin_actions_on_own_order(self):
        order = self.make_order()
        self.login_as(self.owner_a)
        for action in ("approve", "reject", "create-task"):
            with self.subTest(action=action):
                self.assertEqual(self.client.post(f"{URL}/{order.pk}/{action}", {}, format="json").status_code, 403)

    def test_owner_household_field_is_ignored_not_looked_up(self):
        for household in (999999, self.household_b.pk, "abc"):
            with self.subTest(household=household):
                response = self.create(household=household)
                self.assertEqual(response.status_code, 201, response.data)
                self.assertEqual(response.data["household"]["id"], self.household_a.pk)
        response = self.create(self.admin, household=999999)
        self.assertEqual(response.status_code, 400)
        self.assertIn("household", response.data)

    def test_approve_without_logistics_and_task_flag_false_works(self):
        order = self.make_order()
        with no_logistics():
            response = self.approve(order, create_task=False)
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["status"], "APPROVED")


class OrderLifecycleVerifyTests(OrdersBase):
    """Real logistics services: the order follows its task; stock leaves the warehouse exactly once."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls._media = tempfile.mkdtemp(prefix="nnp-orders-media-")
        cls._override = override_settings(MEDIA_ROOT=cls._media, FILE_STORAGE="local")
        cls._override.enable()

    @classmethod
    def tearDownClass(cls):
        cls._override.disable()
        shutil.rmtree(cls._media, ignore_errors=True)
        super().tearDownClass()

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        if not bridge.available():
            return
        from logistics.models import Driver, Vehicle

        cls.vehicle = Vehicle.objects.create(plate_number="01 A 123 BC", model="Isuzu NPR", capacity_kg=Decimal("3000"))
        cls.driver = Driver.objects.create(full_name="Sardor Rahimov", phone="+998901112233", user=cls.logist)

    def setUp(self):
        super().setUp()
        if not bridge.available():
            self.skipTest("logistics.services.create_task_from_order is not available yet")

    def act(self, user, task_id, action, body=None):
        self.login_as(user)
        return self.client.post(f"/api/logistics/tasks/{task_id}/{action}", body or {}, format="json")

    def deliver(self, task_id):
        self.login_as(self.logist)
        upload = self.client.post(
            "/api/files", {"file": SimpleUploadedFile("dalil.png", PNG), "purpose": "DELIVERY_PROOF"},
            format="multipart",
        )
        self.assertEqual(upload.status_code, 201, upload.data)
        response = self.act(self.logist, task_id, "deliver", {"file_ids": [upload.data["id"]], "recipient_name": "Bobur"})
        self.assertEqual(response.status_code, 200, response.data)

    def order_view(self, order, user=None):
        self.login_as(user or self.owner_a)
        response = self.client.get(f"{URL}/{order.pk}")
        self.assertEqual(response.status_code, 200)
        return response.data

    def approved_with_staff(self, quantity=200, approved=150):
        order = self.make_order(items=[{"product": self.feed.pk, "quantity": quantity}])
        with self.captureOnCommitCallbacks(execute=True):
            response = self.approve(order, qty=approved, create_task=True,
                                    task={"vehicle": self.vehicle.pk, "driver": self.driver.pk})
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["task"]["status"], "ASSIGNED")
        return order, response.data["task"]["id"]

    def test_full_cycle_follows_task_and_stock_leaves_once(self):
        balance = Product.objects.get(pk=self.feed.pk).balance
        order, task_id = self.approved_with_staff()
        self.assertTrue(Notification.objects.filter(event="driver_assigned", recipient=self.logist).exists())
        self.assertEqual(self.order_view(order)["task"]["code"], f"LG-{task_id:06d}")

        self.assertEqual(self.act(self.logist, task_id, "accept").status_code, 200)
        order.refresh_from_db()
        self.assertEqual(order.status, OrderStatus.APPROVED)

        self.assertEqual(self.act(self.logist, task_id, "start").status_code, 200)
        self.assertEqual(self.act(self.logist, task_id, "start").status_code, 409)  # replay: no second OUT
        order.refresh_from_db()
        self.assertEqual(order.status, OrderStatus.IN_DELIVERY)
        outs = StockTransaction.objects.filter(type=StockTransactionType.OUT, product=self.feed)
        self.assertEqual([tx.quantity for tx in outs], [Decimal("150.00")])
        feed = Product.objects.get(pk=self.feed.pk)
        self.assertEqual((feed.balance, feed.reserved), (balance - 150, Decimal("0")))

        # While delivering nobody can cancel the order; the owner sees no buttons.
        self.login_as(self.admin)
        response = self.client.post(f"{URL}/{order.pk}/cancel", {"reason": "kech qoldi"}, format="json")
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.data["detail"], services.MSG_DELIVERY_STARTED)
        self.assertEqual(self.order_view(order, self.admin)["allowed_actions"], [])
        self.assertEqual(self.order_view(order)["allowed_actions"], [])

        self.deliver(task_id)
        order.refresh_from_db()
        self.assertEqual(order.status, OrderStatus.DELIVERED)
        view = self.order_view(order)
        self.assertEqual(view["allowed_actions"], ["confirm_receipt"])
        self.login_as(self.owner_a)
        listed = self.client.get(URL).data["results"][0]
        self.assertEqual(listed["allowed_actions"], ["confirm_receipt"])
        self.assertEqual(self.order_view(order, self.admin)["allowed_actions"], ["confirm_receipt"])
        self.assertEqual(self.order_view(order, self.warehouse)["allowed_actions"], [])

        response = self.act(self.owner_a, task_id, "confirm", {"received_ok": False, "issue_note": "2 qop ho‘l"})
        self.assertEqual(response.status_code, 200, response.data)
        order.refresh_from_db()
        self.assertEqual(order.status, OrderStatus.CONFIRMED)
        view = self.order_view(order)
        self.assertEqual(view["allowed_actions"], [])
        self.assertEqual([h["to_status"] for h in view["history"]],
                         ["NEW", "APPROVED", "IN_DELIVERY", "DELIVERED", "CONFIRMED"])
        self.assertIn("muammo bilan qabul qilindi — 2 qop ho‘l", view["history"][-1]["note"])

        # One audit row per order change: create + 4 status changes; stock OUT happened once overall.
        self.assertEqual(order_audits(order).filter(action="create").count(), 1)
        self.assertEqual(order_audits(order).filter(action="status_change").count(), 4)
        self.assertEqual(StockTransaction.objects.filter(type=StockTransactionType.OUT).count(), 1)

        # Owner B never sees it, not even through the task.
        self.login_as(self.owner_b)
        self.assertEqual(self.client.get(f"{URL}/{order.pk}").status_code, 404)
        self.assertEqual(self.client.get(f"/api/logistics/tasks/{task_id}").status_code, 404)

    def test_task_cancelled_in_transit_returns_goods_then_order_can_be_cancelled(self):
        balance = Product.objects.get(pk=self.feed.pk).balance
        order, task_id = self.approved_with_staff()
        self.assertEqual(self.act(self.logist, task_id, "accept").status_code, 200)
        self.assertEqual(self.act(self.logist, task_id, "start").status_code, 200)
        self.assertEqual(self.act(self.admin, task_id, "cancel", {"reason": "Mashina buzildi"}).status_code, 200)
        order.refresh_from_db()
        self.assertEqual(order.status, OrderStatus.APPROVED)
        self.assertEqual(Product.objects.get(pk=self.feed.pk).balance, balance)  # returned with one IN

        view = self.order_view(order, self.admin)
        self.assertIsNone(view["task"])
        self.assertEqual(view["allowed_actions"], ["cancel", "create_task"])
        with self.captureOnCommitCallbacks(execute=True):
            response = self.client.post(f"{URL}/{order.pk}/cancel", {"reason": "Mijoz voz kechdi"}, format="json")
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["status"], "CANCELLED")
        rows = list(Notification.objects.filter(event="order_cancelled").values_list("recipient_id", "button_path"))
        self.assertEqual(rows, [(self.owner_a.pk, f"/my/orders/{order.pk}")])
        self.assertEqual(Product.objects.get(pk=self.feed.pk).balance, balance)
        self.assertEqual(Product.objects.get(pk=self.feed.pk).reserved, Decimal("0"))

    def test_approve_with_bad_staff_saves_nothing(self):
        order = self.make_order()
        with self.captureOnCommitCallbacks(execute=True):
            response = self.approve(order, create_task=True, task={"vehicle": self.vehicle.pk})
        self.assertEqual(response.status_code, 400, response.data)
        order.refresh_from_db()
        self.assertEqual(order.status, OrderStatus.NEW)
        self.assertFalse(order.items.filter(approved_qty__isnull=False).exists())
        self.assertFalse(order_audits(order).filter(action="status_change").exists())
        self.assertFalse(Notification.objects.filter(event__in=["order_approved", "task_created"]).exists())
        self.assertEqual(Product.objects.get(pk=self.feed.pk).reserved, Decimal("0"))

    def test_approved_notification_lists_only_given_items(self):
        order = self.make_order(items=[{"product": self.feed.pk, "quantity": 200},
                                       {"product": self.vitamin.pk, "quantity": 3}])
        feed_item, vitamin_item = order.items.order_by("id")
        self.login_as(self.admin)
        body = {"items": [{"id": feed_item.pk, "approved_qty": 150}, {"id": vitamin_item.pk, "approved_qty": 0}],
                "create_task": True}
        with self.captureOnCommitCallbacks(execute=True):
            self.assertEqual(self.client.post(f"{URL}/{order.pk}/approve", body, format="json").status_code, 200)
        row = Notification.objects.get(event="order_approved")
        self.assertEqual((row.recipient_id, row.button_path), (self.owner_a.pk, f"/my/orders/{order.pk}"))
        self.assertIn("Mahsulotlar: Yem PK-1 — 150 kg", row.text)
        self.assertNotIn("Vitamin", row.text)
        self.assertIn("Operatsiya ID: #" + order.code, row.text)

    def test_list_query_budget_with_tasks(self):
        def count() -> int:
            self.login_as(self.admin)
            with CaptureQueriesContext(connection) as ctx:
                self.assertEqual(self.client.get(URL).status_code, 200)
            return len(ctx.captured_queries)

        self.approved_with_staff(quantity=10, approved=10)
        small = count()
        for _ in range(5):
            self.approved_with_staff(quantity=10, approved=10)
        self.assertEqual(count(), small)
        self.assertLessEqual(small, 6)


class ConcurrentApproveTests(TransactionTestCase):
    """Two admins approve the same order with a task at the same time: one wins, stock is reserved once."""

    def setUp(self) -> None:
        if not bridge.available():
            self.skipTest("logistics.services.create_task_from_order is not available yet")
        from logistics.tests.helpers import stock_in

        self.admin = make_user("admin_oc", Role.ADMIN)
        household = make_household("Navbahor 7-uy", "owner_oc")
        self.feed = make_product("Yem PK-7", min_stock="0")
        stock_in(self.admin, self.feed, "500")
        order = services.create_order(
            actor=household.owner,
            kind="SUPPLY_REQUEST",
            items=[services.ItemInput(product=self.feed, quantity=Decimal("120"))],
        )
        self.order_id = order.pk
        self.item_id = order.items.get().pk

    def test_two_threads_one_approval_one_task(self):
        from logistics.models import LogisticsTask

        barrier = threading.Barrier(2)
        outcomes: list[str] = []
        lock = threading.Lock()

        def worker() -> None:
            try:
                barrier.wait(timeout=10)
                services.approve_order(
                    actor=self.admin,
                    order_id=self.order_id,
                    items=[{"id": self.item_id, "approved_qty": Decimal("100")}],
                    create_task=True,
                )
                result = "ok"
            except services.Conflict:
                result = "conflict"
            except OperationalError:  # shared in-memory test DB: the loser may see "table is locked"
                result = "locked"
            finally:
                connections.close_all()
            with lock:
                outcomes.append(result)

        threads = [threading.Thread(target=worker) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=60)

        self.assertEqual(len(outcomes), 2, outcomes)
        self.assertEqual(outcomes.count("ok"), 1, outcomes)
        self.assertEqual(LogisticsTask.objects.filter(order_id=self.order_id).count(), 1)
        self.assertEqual(Product.objects.get(pk=self.feed.pk).reserved, Decimal("100"))
        self.assertEqual(
            OrderStatusLog.objects.filter(order_id=self.order_id, to_status=OrderStatus.APPROVED).count(), 1
        )

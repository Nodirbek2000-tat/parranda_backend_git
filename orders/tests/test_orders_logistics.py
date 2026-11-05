"""Orders ↔ logistics through ``orders/bridge.py`` (API_CONTRACT_V3 §6.2, §6.4, §7.4) with the real logistics
services: approve + task creation in one transaction, stock reservation, rollback on short stock, order
cancel releasing the reservation, task cancel returning the order to ``APPROVED``.

Skipped while the logistics module does not provide ``create_task_from_order`` yet (parallel development).
"""

from __future__ import annotations

from decimal import Decimal

from audit.models import AuditLog
from notifications.models import Notification
from orders import bridge
from orders.models import Order, OrderStatus
from warehouse.models import Product

from .test_orders import URL, OrdersBase


class OrderLogisticsTests(OrdersBase):
    def setUp(self):
        super().setUp()
        if not bridge.available():
            self.skipTest("logistics.services.create_task_from_order is not available yet")
        from logistics.models import LogisticsTask

        self.Task = LogisticsTask

    def reserved(self, product) -> Decimal:
        return Product.objects.get(pk=product.pk).reserved

    def test_approve_creates_task_and_reserves_stock(self):
        order = self.make_order(items=[{"product": self.feed.pk, "quantity": 200},
                                       {"product": self.vitamin.pk, "quantity": 3}])
        feed_item, vitamin_item = order.items.order_by("id")
        self.login_as(self.admin)
        body = {"items": [{"id": feed_item.pk, "approved_qty": 150}, {"id": vitamin_item.pk, "approved_qty": 0}],
                "task": {"note": "Ertalab"}}
        with self.captureOnCommitCallbacks(execute=True):
            response = self.client.post(f"{URL}/{order.pk}/approve", body, format="json")
        self.assertEqual(response.status_code, 200, response.data)
        task = self.Task.objects.get(order_id=order.pk)
        self.assertEqual(response.data["status"], "APPROVED")
        self.assertEqual(response.data["task"], {"id": task.pk, "code": task.code, "status": "NEW",
                                                 "status_display": "Yangi"})
        self.assertEqual(response.data["allowed_actions"], ["cancel"])
        self.assertEqual([(i.product_id, i.quantity, i.order_item_id) for i in task.items.all()],
                         [(self.feed.pk, Decimal("150.00"), feed_item.pk)])
        self.assertEqual(self.reserved(self.feed), Decimal("150"))
        self.assertEqual(self.reserved(self.vitamin), Decimal("0"))
        self.assertTrue(Notification.objects.filter(event="task_created", recipient=self.logist).exists())
        self.assertIn("LG-", Notification.objects.get(event="order_approved").text)

        # Order list shows the task without extra queries per row.
        self.assertEqual(self.client.get(URL).data["results"][0]["task"]["id"], task.pk)
        # A second task for the same order is refused.
        response = self.client.post(f"{URL}/{order.pk}/create-task", {}, format="json")
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.data["detail"], "Bu buyurtma uchun faol logistika topshirig‘i allaqachon mavjud.")

    def test_inactive_household_or_product_rolls_back_the_approval(self):
        from farm.models import Household

        order = self.make_order(items=[{"product": self.feed.pk, "quantity": 20}])
        Product.objects.filter(pk=self.feed.pk).update(is_active=False)
        with self.captureOnCommitCallbacks(execute=True):
            response = self.approve(order, create_task=True)
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.data, {"items": ["“Yem PK-1” mahsulot faol emas."]})
        Product.objects.filter(pk=self.feed.pk).update(is_active=True)
        Household.objects.filter(pk=order.household_id).update(is_active=False)
        with self.captureOnCommitCallbacks(execute=True):
            response = self.approve(order, create_task=True)
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.data, {"household": ["Xonadon faol emas"]})
        order.refresh_from_db()
        self.assertEqual(order.status, OrderStatus.NEW)
        self.assertFalse(self.Task.objects.exists())
        self.assertEqual(self.reserved(self.feed), Decimal("0"))

    def test_short_stock_rolls_back_the_whole_approval(self):
        order = self.make_order(items=[{"product": self.feed.pk, "quantity": 2000}])
        with self.captureOnCommitCallbacks(execute=True):
            response = self.approve(order, create_task=True)
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.data, {"items": ["“Yem PK-1”: omborda yetarli emas. Bo‘sh qoldiq: 1 000 kg"]})
        order.refresh_from_db()
        self.assertEqual(order.status, OrderStatus.NEW)
        self.assertFalse(order.items.filter(approved_qty__isnull=False).exists())
        self.assertFalse(self.Task.objects.exists())
        self.assertEqual(self.reserved(self.feed), Decimal("0"))
        self.assertFalse(AuditLog.objects.filter(action="status_change").exists())
        self.assertFalse(Notification.objects.filter(event__in=["order_approved", "task_created"]).exists())

    def test_create_task_later_then_cancel_order_releases_reservation(self):
        order = self.make_order()
        self.approve(order, create_task=False)
        self.login_as(self.admin)
        response = self.client.post(f"{URL}/{order.pk}/create-task", {"note": "Keyinroq"}, format="json")
        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(response.data["id"], self.Task.objects.get(order_id=order.pk).pk)
        self.assertEqual(self.reserved(self.feed), Decimal("200"))

        with self.captureOnCommitCallbacks(execute=True):
            response = self.client.post(f"{URL}/{order.pk}/cancel", {"reason": "Xonadon rad etdi"}, format="json")
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["status"], "CANCELLED")
        self.assertIsNone(response.data["task"])
        task = self.Task.objects.get(order_id=order.pk)
        self.assertEqual(task.status, "CANCELLED")
        self.assertEqual(self.reserved(self.feed), Decimal("0"))
        self.assertEqual(
            list(Order.objects.get(pk=order.pk).status_logs.values_list("to_status", flat=True)),
            ["NEW", "APPROVED", "CANCELLED"],
        )

    def test_task_cancel_returns_order_to_approved(self):
        from logistics import services as logistics

        order = self.make_order()
        self.approve(order, create_task=True)
        task = self.Task.objects.get(order_id=order.pk)
        self.login_as(self.admin)
        response = self.client.post(f"/api/logistics/tasks/{task.pk}/cancel", {"reason": "Mashina buzildi"},
                                    format="json")
        if response.status_code == 404:  # task endpoints not wired yet: call the service directly
            logistics.cancel_task(actor=self.admin, task_id=task.pk, reason="Mashina buzildi")
        order.refresh_from_db()
        self.assertEqual(order.status, OrderStatus.APPROVED)
        self.assertEqual(self.reserved(self.feed), Decimal("0"))
        detail = self.client.get(f"{URL}/{order.pk}").data
        self.assertIsNone(detail["task"])
        self.assertEqual(detail["allowed_actions"], ["cancel", "create_task"])

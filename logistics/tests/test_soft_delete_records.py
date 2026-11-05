"""Soft delete of batches, orders, logistics tasks and inventory counts (DELETE on the detail routes).

The row stays for history (FKs from movements, tasks, audit still load it), disappears from lists / details /
search / dashboards, and is refused (409) only while deleting would break running work or bird / stock balances.
"""

from __future__ import annotations

import io
from datetime import timedelta
from decimal import Decimal

from django.core.management import call_command
from django.utils import timezone

from audit.models import AuditAction, AuditLog
from common.testing import make_batch
from farm import services as farm_services
from farm.models import MovementType, PoultryBatch, PoultryMovement
from logistics.models import LogisticsTask
from orders.models import Order, OrderKind, OrderStatus
from warehouse.models import InventoryCount, Product, StockTransaction

from .helpers import LogisticsTestCase


def _ids(response) -> list[int]:
    data = response.data
    rows = data["results"] if isinstance(data, dict) and "results" in data else data
    return [row["id"] for row in rows]


def _search_ids(response, group: str) -> list[int]:
    for item in response.data["groups"]:
        if item["type"] == group:
            return [row["id"] for row in item["items"]]
    return []


class IntegrityMixin:
    def assert_integrity(self) -> None:
        out = io.StringIO()
        call_command("check_integrity", stdout=out)  # raises CommandError on any mismatch
        self.assertIn("Nomuvofiqlik topilmadi", out.getvalue())


# --- Batches ----------------------------------------------------------------------------------------


class BatchDeleteTests(IntegrityMixin, LogisticsTestCase):
    def _returned_batch(self) -> PoultryBatch:
        """A batch of 100: 40 went to household A and came back, 5 died in the center → 95 in the center."""
        batch = make_batch(self.admin, 100)
        today = timezone.localdate()
        for kind, household, quantity, days in (
            (MovementType.DISTRIBUTE, self.household_a, 40, 20),
            (MovementType.RETURN, self.household_a, 40, 10),
            (MovementType.DEATH, None, 5, 5),
        ):
            farm_services.record_movement(
                actor=self.admin, batch=batch, household=household, movement_type=kind, quantity=quantity,
                on_date=today - timedelta(days=days),
            )
        return batch

    def test_refused_while_birds_are_in_households(self):
        self.login_as(self.admin)
        response = self.client.delete(f"/api/batches/{self.batch.pk}")
        self.assertEqual(response.status_code, 409)
        self.assertIn("500 ta parrandasi", response.data["detail"])
        self.assertIn(self.household_a.code, response.data["detail"])
        self.assertIsNone(PoultryBatch.all_objects.get(pk=self.batch.pk).deleted_at)

    def test_delete_hides_batch_and_its_movements_consistently(self):
        batch = self._returned_batch()
        self.login_as(self.admin)
        before = self.client.get("/api/dashboard/admin").data
        self.login_as(self.warehouse)
        response = self.client.delete(f"/api/batches/{batch.pk}")
        self.assertEqual(response.status_code, 204)
        self.assertIsNotNone(PoultryBatch.all_objects.get(pk=batch.pk).deleted_at)

        # Hidden: detail 404, list, search, movements list, batch pickers.
        self.assertEqual(self.client.get(f"/api/batches/{batch.pk}").status_code, 404)
        self.assertNotIn(batch.pk, _ids(self.client.get("/api/batches?page_size=200")))
        self.assertNotIn(batch.pk, _search_ids(self.client.get(f"/api/search?q={batch.code}"), "batch"))
        self.assertEqual(_ids(self.client.get(f"/api/movements?batch={batch.pk}")), [])
        response = self.client.post(
            "/api/movements",
            {"batch": batch.pk, "household": self.household_a.pk, "type": "DISTRIBUTE", "quantity": 1},
            format="json",
        )
        self.assertEqual(response.status_code, 400)

        # Dashboard: the batch's 95 center birds leave together with its movements; households unchanged.
        self.login_as(self.admin)
        after = self.client.get("/api/dashboard/admin").data
        self.assertEqual(after["birds_in_center"], before["birds_in_center"] - 95)
        self.assertEqual(after["birds_in_households"], before["birds_in_households"])
        self.assertEqual(self.client.get("/api/stats/overview").status_code, 200)

        # History: movements still exist and load the batch through the FK.
        movement = PoultryMovement.all_objects.filter(batch_id=batch.pk).first()
        self.assertEqual(movement.batch.pk, batch.pk)
        self.assertTrue(AuditLog.objects.filter(action=AuditAction.DELETE, object_id=batch.pk, module="batches").exists())
        self.assert_integrity()

        # The code is not reused by the next batch of the year.
        new_batch = make_batch(self.admin, 10)
        self.assertNotEqual(new_batch.code, batch.code)

    def test_permissions(self):
        batch = make_batch(self.admin, 10)
        for user in (self.vet, self.owner_a, self.logist):
            self.login_as(user)
            self.assertEqual(self.client.delete(f"/api/batches/{batch.pk}").status_code, 403)
        self.assertIsNone(PoultryBatch.all_objects.get(pk=batch.pk).deleted_at)
        self.login_as(self.admin)
        self.assertEqual(self.client.delete(f"/api/batches/{batch.pk}").status_code, 204)
        self.assertEqual(self.client.delete(f"/api/batches/{batch.pk}").status_code, 404)


# --- Orders -----------------------------------------------------------------------------------------


class OrderDeleteBase(IntegrityMixin, LogisticsTestCase):
    def make_order(self, owner=None, quantity: int = 20) -> Order:
        self.login_as(owner or self.owner_a)
        response = self.client.post(
            "/api/orders",
            {"kind": OrderKind.SUPPLY_REQUEST, "items": [{"product": self.feed.pk, "quantity": quantity}]},
            format="json",
        )
        self.assertEqual(response.status_code, 201, response.data)
        return Order.objects.get(pk=response.data["id"])

    def approve_with_task(self, order: Order) -> LogisticsTask:
        self.login_as(self.admin)
        items = [{"id": item.pk, "approved_qty": item.requested_qty} for item in order.items.order_by("id")]
        response = self.client.post(
            f"/api/orders/{order.pk}/approve", {"items": items, "create_task": True}, format="json"
        )
        self.assertEqual(response.status_code, 200, response.data)
        return LogisticsTask.objects.get(order_id=order.pk)


class OrderDeleteTests(OrderDeleteBase):
    def test_owner_deletes_own_new_order(self):
        order = self.make_order()
        self.login_as(self.owner_b)
        self.assertEqual(self.client.delete(f"/api/orders/{order.pk}").status_code, 404)
        self.login_as(self.owner_a)
        self.assertEqual(self.client.delete(f"/api/orders/{order.pk}").status_code, 204)
        self.assertIsNotNone(Order.all_objects.get(pk=order.pk).deleted_at)
        self.assertEqual(self.client.get(f"/api/orders/{order.pk}").status_code, 404)
        self.assertNotIn(order.pk, _ids(self.client.get("/api/orders")))
        self.login_as(self.admin)
        self.assertEqual(self.client.get(f"/api/orders/{order.pk}").status_code, 404)
        self.assertNotIn(order.pk, _ids(self.client.get("/api/orders?page_size=200")))
        self.assertNotIn(order.pk, _search_ids(self.client.get(f"/api/search?q={order.code}"), "order"))
        self.assertEqual(self.client.get("/api/dashboard/admin").data["orders"]["new"], 0)
        self.assertTrue(AuditLog.objects.filter(action=AuditAction.DELETE, object_id=order.pk, module="orders").exists())

    def test_owner_cannot_delete_decided_order(self):
        order = self.make_order()
        self.login_as(self.admin)
        items = [{"id": item.pk, "approved_qty": item.requested_qty} for item in order.items.order_by("id")]
        response = self.client.post(f"/api/orders/{order.pk}/approve", {"items": items}, format="json")
        self.assertEqual(response.status_code, 200, response.data)
        self.login_as(self.owner_a)
        response = self.client.delete(f"/api/orders/{order.pk}")
        self.assertEqual(response.status_code, 409)
        self.assertIn("yangi", response.data["detail"])
        self.assertIsNone(Order.all_objects.get(pk=order.pk).deleted_at)

    def test_admin_refused_while_task_not_finished_then_allowed(self):
        order = self.make_order()
        task = self.approve_with_task(order)
        self.login_as(self.admin)
        response = self.client.delete(f"/api/orders/{order.pk}")
        self.assertEqual(response.status_code, 409)
        self.assertIn(task.code, response.data["detail"])
        # On the way → refused as well.
        self.to_status(task.pk, "IN_TRANSIT")
        self.login_as(self.admin)
        response = self.client.delete(f"/api/orders/{order.pk}")
        self.assertEqual(response.status_code, 409)
        # Delivered and confirmed → the finished order may be hidden; the task keeps its order link.
        file_id = self.upload_proof(self.logist)
        self.assertEqual(self.act(self.logist, task.pk, "deliver", {"file_ids": [file_id]}).status_code, 200)
        self.assertEqual(self.act(self.owner_a, task.pk, "confirm", {"received_ok": True}).status_code, 200)
        self.login_as(self.admin)
        self.assertEqual(self.client.delete(f"/api/orders/{order.pk}").status_code, 204)
        task = LogisticsTask.objects.get(pk=task.pk)
        self.assertEqual(task.order.pk, order.pk)
        self.assertEqual(task.order.status, OrderStatus.CONFIRMED)
        response = self.client.get(f"/api/logistics/tasks/{task.pk}")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["order"]["id"], order.pk)
        # A transition attempt on the finished task of a deleted order is a clean 409 (not a 500).
        self.assertEqual(self.act(self.admin, task.pk, "cancel", {"reason": "Kerak emas"}).status_code, 409)
        self.assert_integrity()

    def test_permissions(self):
        order = self.make_order()
        for user in (self.warehouse, self.vet, self.logist):
            self.login_as(user)
            self.assertEqual(self.client.delete(f"/api/orders/{order.pk}").status_code, 403)
        self.assertIsNone(Order.all_objects.get(pk=order.pk).deleted_at)
        self.login_as(self.admin)
        self.assertEqual(self.client.delete(f"/api/orders/{order.pk}").status_code, 204)


# --- Logistics tasks -------------------------------------------------------------------------------


class TaskDeleteTests(OrderDeleteBase):
    def reserved(self) -> Decimal:
        return Product.objects.get(pk=self.feed.pk).reserved

    def test_new_task_is_cancelled_then_hidden(self):
        task = self.create_task(quantity=30)
        self.assertEqual(self.reserved(), Decimal("30"))
        self.login_as(self.admin)
        response = self.client.delete(f"/api/logistics/tasks/{task['id']}")
        self.assertEqual(response.status_code, 204)
        row = LogisticsTask.all_objects.get(pk=task["id"])
        self.assertIsNotNone(row.deleted_at)
        self.assertEqual(row.status, "CANCELLED")
        self.assertEqual(self.reserved(), Decimal("0"))
        self.assertEqual(self.client.get(f"/api/logistics/tasks/{task['id']}").status_code, 404)
        self.assertNotIn(task["id"], _ids(self.client.get("/api/logistics/tasks?page_size=200")))
        self.assertNotIn(task["id"], _search_ids(self.client.get(f"/api/search?q={task['code']}"), "task"))
        self.assertEqual(self.client.get("/api/logistics/summary").status_code, 200)
        self.assertEqual(self.act(self.admin, task["id"], "assign",
                                  {"vehicle": self.vehicle.pk, "driver": self.driver.pk}).status_code, 404)
        self.assertTrue(AuditLog.objects.filter(action=AuditAction.DELETE, object_id=task["id"],
                                                module="logistics").exists())
        self.assert_integrity()

    def test_new_task_of_order_returns_order_to_approved(self):
        order = self.make_order()
        task = self.approve_with_task(order)
        self.login_as(self.admin)
        self.assertEqual(self.client.delete(f"/api/logistics/tasks/{task.pk}").status_code, 204)
        self.assertEqual(Order.objects.get(pk=order.pk).status, OrderStatus.APPROVED)
        self.assertEqual(self.reserved(), Decimal("0"))
        # The order can get a new task.
        response = self.client.post(f"/api/orders/{order.pk}/create-task", {}, format="json")
        self.assertEqual(response.status_code, 201, response.data)
        self.assert_integrity()

    def test_refused_while_driver_works_on_it(self):
        task = self.create_task(quantity=10)
        self.to_status(task["id"], "ASSIGNED")
        for status, action in (("ASSIGNED", None), ("ACCEPTED", "accept"), ("IN_TRANSIT", "start")):
            if action:
                self.assertEqual(self.act(self.logist, task["id"], action).status_code, 200)
            self.login_as(self.admin)
            response = self.client.delete(f"/api/logistics/tasks/{task['id']}")
            self.assertEqual(response.status_code, 409, status)
            self.assertIn("haydovchi", response.data["detail"])
        self.assertIsNone(LogisticsTask.all_objects.get(pk=task["id"]).deleted_at)

    def test_delivered_task_of_order_refused_until_confirmed(self):
        order = self.make_order()
        task = self.approve_with_task(order)
        self.to_status(task.pk, "DELIVERED")
        self.login_as(self.admin)
        response = self.client.delete(f"/api/logistics/tasks/{task.pk}")
        self.assertEqual(response.status_code, 409)
        self.assertIn(order.code, response.data["detail"])
        self.assertEqual(self.act(self.owner_a, task.pk, "confirm", {"received_ok": True}).status_code, 200)
        self.login_as(self.admin)
        self.assertEqual(self.client.delete(f"/api/logistics/tasks/{task.pk}").status_code, 204)
        # History: the stock OUT still points at the task's product, the order detail still loads.
        self.assertTrue(StockTransaction.objects.filter(reference_id=task.pk, type="OUT").exists())
        self.assertEqual(self.client.get(f"/api/orders/{order.pk}").status_code, 200)
        self.assertEqual(LogisticsTask.all_objects.get(pk=task.pk).vehicle.pk, self.vehicle.pk)
        self.assert_integrity()

    def test_delivered_task_without_order_may_be_deleted(self):
        task = self.create_task(quantity=10)
        self.to_status(task["id"], "DELIVERED")
        self.login_as(self.admin)
        self.assertEqual(self.client.delete(f"/api/logistics/tasks/{task['id']}").status_code, 204)
        # The vehicle is free again (no visible not-finished task).
        self.login_as(self.logist)
        self.assertEqual(self.client.delete(f"/api/logistics/vehicles/{self.vehicle.pk}").status_code, 204)
        self.assert_integrity()

    def test_permissions(self):
        task = self.create_task(quantity=10)
        for user in (self.warehouse, self.logist, self.owner_a, self.vet):
            self.login_as(user)
            self.assertEqual(self.client.delete(f"/api/logistics/tasks/{task['id']}").status_code, 403)
        self.assertIsNone(LogisticsTask.all_objects.get(pk=task["id"]).deleted_at)


# --- Inventory counts ------------------------------------------------------------------------------


class InventoryDeleteTests(IntegrityMixin, LogisticsTestCase):
    def create_count(self) -> dict:
        self.login_as(self.warehouse)
        response = self.client.post("/api/inventory-counts", {"note": "Oy oxiri"}, format="json")
        self.assertEqual(response.status_code, 201, response.data)
        return response.data

    def test_draft_deleted_by_warehouse(self):
        count = self.create_count()
        self.assertEqual(self.client.delete(f"/api/inventory-counts/{count['id']}").status_code, 204)
        self.assertIsNotNone(InventoryCount.all_objects.get(pk=count["id"]).deleted_at)
        self.assertEqual(self.client.get(f"/api/inventory-counts/{count['id']}").status_code, 404)
        self.assertNotIn(count["id"], _ids(self.client.get("/api/inventory-counts")))
        response = self.client.patch(f"/api/inventory-counts/{count['id']}/lines", {"lines": []}, format="json")
        self.assertEqual(response.status_code, 404)
        self.assertTrue(AuditLog.objects.filter(action=AuditAction.DELETE, object_id=count["id"],
                                                module="inventory").exists())

    def test_approved_count_is_only_hidden_by_admin(self):
        count = self.create_count()
        lines = [{"product": line["product"]["id"], "counted_qty": 7} for line in count["lines"]]
        self.assertEqual(
            self.client.patch(f"/api/inventory-counts/{count['id']}/lines", {"lines": lines}, format="json").status_code,
            200,
        )
        self.login_as(self.admin)
        self.assertEqual(self.client.post(f"/api/inventory-counts/{count['id']}/approve").status_code, 200)
        balances = dict(Product.objects.values_list("id", "balance"))
        adjustments = StockTransaction.objects.filter(type__in=["ADJUST_IN", "ADJUST_OUT"]).count()
        self.assertGreater(adjustments, 0)

        self.login_as(self.warehouse)
        response = self.client.delete(f"/api/inventory-counts/{count['id']}")
        self.assertEqual(response.status_code, 403)
        self.login_as(self.admin)
        self.assertEqual(self.client.delete(f"/api/inventory-counts/{count['id']}").status_code, 204)
        # The posted adjustments and balances stay as they are.
        self.assertEqual(dict(Product.objects.values_list("id", "balance")), balances)
        self.assertEqual(StockTransaction.objects.filter(type__in=["ADJUST_IN", "ADJUST_OUT"]).count(), adjustments)
        self.assertEqual(self.client.get(f"/api/inventory-counts/{count['id']}").status_code, 404)
        self.assert_integrity()

    def test_permissions(self):
        count = self.create_count()
        for user in (self.vet, self.owner_a, self.logist):
            self.login_as(user)
            self.assertEqual(self.client.delete(f"/api/inventory-counts/{count['id']}").status_code, 403)
        self.assertIsNone(InventoryCount.all_objects.get(pk=count["id"]).deleted_at)

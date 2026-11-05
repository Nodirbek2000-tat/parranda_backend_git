"""Orders (API_CONTRACT_V3 §6, §5.5, §10.1/10.2 orders keys, §12 order group, §19.1 “Orders”)."""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal
from types import SimpleNamespace
from unittest import mock

from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from accounts.models import User
from audit.models import AuditLog
from common.roles import Role
from common.testing import BaseAPITestCase, make_product, make_user
from farm.models import Household
from notifications.models import Notification
from orders import bridge, selectors, services
from orders.models import Order, OrderItem, OrderKind, OrderStatus, OrderStatusLog
from warehouse.models import Product, ProductCategory, Unit

URL = "/api/orders"


def no_logistics():
    """Approve / create-task as if the logistics module were not deployed yet."""
    return mock.patch.object(bridge, "available", return_value=False)


class OrdersBase(BaseAPITestCase):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.logist = make_user("logist1", Role.LOGISTICS)
        cls.vet = make_user("vet1", Role.VET)
        cls.admin2 = make_user("admin2", Role.ADMIN, telegram_id=7101)
        User.objects.filter(pk=cls.owner_a.pk).update(telegram_id=8101)
        cls.owner_a.refresh_from_db()
        cls.feed = make_product("Yem PK-1")
        cls.vitamin = make_product("Vitamin C", unit=Unit.PIECE)
        cls.vaccine = Product.objects.create(name="Vaksina ND", category=ProductCategory.VACCINE, unit=Unit.PIECE)
        Product.objects.filter(pk__in=[cls.feed.pk, cls.vitamin.pk]).update(balance=Decimal("1000"))

    def create(self, user=None, **body):
        self.login_as(user or self.owner_a)
        payload = {"kind": OrderKind.SUPPLY_REQUEST, "items": [{"product": self.feed.pk, "quantity": 200}]}
        payload.update(body)
        return self.client.post(URL, payload, format="json")

    def make_order(self, user=None, **body) -> Order:
        response = self.create(user, **body)
        self.assertEqual(response.status_code, 201, response.data)
        return Order.objects.get(pk=response.data["id"])

    def approve(self, order: Order, *, qty=None, create_task=False, user=None, **extra):
        self.login_as(user or self.admin)
        items = [{"id": item.pk, "approved_qty": qty if qty is not None else item.requested_qty}
                 for item in order.items.order_by("id")]
        body = {"items": items, "create_task": create_task, **extra}
        return self.client.post(f"{URL}/{order.pk}/approve", body, format="json")


class OrderCreateTests(OrdersBase):
    def test_owner_creates_for_own_household(self):
        with self.captureOnCommitCallbacks(execute=True):
            response = self.create(
                note="Yem tugayapti",
                desired_date=str(timezone.localdate() + timedelta(days=2)),
                household=self.household_b.pk,  # ignored for owners
                items=[{"product": self.feed.pk, "quantity": 200}, {"product": self.vitamin.pk, "quantity": 2,
                                                                     "note": "tezroq"}],
            )
        self.assertEqual(response.status_code, 201, response.data)
        data = response.data
        self.assertRegex(data["code"], r"^BY-\d{6}$")
        self.assertEqual(data["household"]["id"], self.household_a.pk)
        self.assertEqual((data["status"], data["status_display"]), ("NEW", "Yangi"))
        self.assertEqual(data["kind_display"], "Yem yoki mahsulot talabi")
        self.assertEqual([i["requested_qty"] for i in data["items"]], [200.0, 2.0])
        self.assertEqual(data["items"][1]["note"], "tezroq")
        self.assertEqual(data["items"][0]["product"], {"id": self.feed.pk, "name": "Yem PK-1", "unit": "kg",
                                                      "category": "FEED"})
        self.assertIsNone(data["task"])
        self.assertEqual(data["allowed_actions"], ["cancel"])
        self.assertEqual(len(data["history"]), 1)
        self.assertEqual(data["history"][0]["to_status"], "NEW")
        self.assertEqual(data["history"][0]["actor"]["id"], self.owner_a.pk)

        log = AuditLog.objects.get(module="orders", action="create")
        self.assertEqual((log.object_type, log.object_id, log.op_id), ("Order", data["id"], data["code"]))
        self.assertEqual(log.after["status"], "NEW")
        self.assertEqual(len(log.after["items"]), 2)

        rows = {n.recipient_id: n for n in Notification.objects.filter(event="order_created")}
        self.assertEqual(set(rows), {self.admin.pk, self.admin2.pk})
        self.assertEqual(rows[self.admin2.pk].status, "PENDING")
        self.assertEqual(rows[self.admin.pk].status, "SKIPPED")  # no Telegram
        self.assertEqual(rows[self.admin.pk].button_path, f"/orders/{data['id']}")
        self.assertIn("Yem PK-1 — 200 kg; Vitamin C — 2 dona", rows[self.admin.pk].text)
        self.assertIn("Turi: Yem yoki mahsulot talabi", rows[self.admin.pk].text)

    def test_notification_only_after_commit(self):
        with self.captureOnCommitCallbacks(execute=False) as callbacks:
            self.assertEqual(self.create().status_code, 201)
        self.assertFalse(Notification.objects.filter(event="order_created").exists())
        for callback in callbacks:
            callback()
        self.assertTrue(Notification.objects.filter(event="order_created").exists())

    def test_admin_needs_household(self):
        response = self.create(self.admin)
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.data, {"household": ["Xonadonni tanlang."]})
        response = self.create(self.admin, household=self.household_b.pk, kind=OrderKind.PRODUCT_ORDER)
        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.data["household"]["id"], self.household_b.pk)
        self.assertEqual(response.data["allowed_actions"], ["approve", "reject", "cancel"])

    def test_validation_messages(self):
        many = [{"product": make_product(f"P{i}").pk, "quantity": 1} for i in range(21)]
        Product.objects.filter(pk=self.vitamin.pk).update(is_active=False)
        cases = [
            ({"items": []}, {"items": ["Kamida bitta mahsulot tanlang."]}),
            ({"items": many}, {"items": ["Bitta buyurtmada 20 tadan ko‘p mahsulot bo‘lmasin."]}),
            ({"items": [{"product": self.feed.pk, "quantity": 1}, {"product": self.feed.pk, "quantity": 2}]},
             {"items": ["Mahsulot takrorlanmasin."]}),
            ({"items": [{"product": self.vitamin.pk, "quantity": 1}]}, {"items": ["“Vitamin C” mahsulot faol emas."]}),
            ({"items": [{"product": 999999, "quantity": 1}]}, {"items": ["“#999999” mahsulot faol emas."]}),
            ({"items": [{"product": self.feed.pk, "quantity": 0}]}, {"items": ["Miqdor 0 dan katta bo‘lishi kerak"]}),
            ({"items": [{"product": self.vaccine.pk, "quantity": "1.5"}]},
             {"items": ["“Vaksina ND”: “dona” uchun butun son kiriting"]}),
            ({"desired_date": str(timezone.localdate() - timedelta(days=1))},
             {"desired_date": ["Sana o‘tgan kun bo‘lishi mumkin emas."]}),
            ({"kind": "OTHER"}, {"kind": ["“OTHER” ruxsat etilgan qiymat emas."]}),
        ]
        for body, expected in cases:
            with self.subTest(body=str(body)[:60]):
                response = self.create(**body)
                self.assertEqual(response.status_code, 400, response.data)
                self.assertEqual(response.data, expected)
        self.assertFalse(Order.objects.exists())
        self.assertFalse(AuditLog.objects.filter(module="orders").exists())

    def test_inactive_household_owner(self):
        Household.objects.filter(pk=self.household_a.pk).update(is_active=False)
        response = self.create()
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.data,
                         {"non_field_errors": ["Xonadoningiz faol emas. Administratorga murojaat qiling."]})
        response = self.create(self.admin, household=self.household_a.pk)
        self.assertEqual(response.data, {"household": ["Xonadon faol emas"]})

    def test_stock_is_not_checked_at_creation(self):
        Product.objects.filter(pk=self.feed.pk).update(balance=0)
        self.assertEqual(self.create(items=[{"product": self.feed.pk, "quantity": 5000}]).status_code, 201)

    def test_role_matrix(self):
        order = self.make_order()
        for user, expected_get, expected_post in (
            (self.admin, 200, 201),
            (self.warehouse, 200, 403),
            (self.owner_a, 200, 201),
            (self.logist, 403, 403),
            (self.vet, 403, 403),
        ):
            with self.subTest(user=user.username):
                self.login_as(user)
                self.assertEqual(self.client.get(URL).status_code, expected_get)
                self.assertEqual(self.client.get(f"{URL}/{order.pk}").status_code, expected_get)
                body = {"kind": "SUPPLY_REQUEST", "items": [{"product": self.feed.pk, "quantity": 1}],
                        "household": self.household_a.pk}
                self.assertEqual(self.client.post(URL, body, format="json").status_code, expected_post)
        self.login_as(None)
        self.assertEqual(self.client.get(URL).status_code, 401)


class OrderListDetailTests(OrdersBase):
    def test_owner_isolation(self):
        own = self.make_order()
        other = self.make_order(self.owner_b)
        self.login_as(self.owner_a)
        response = self.client.get(URL)
        self.assertEqual([row["id"] for row in response.data["results"]], [own.pk])
        self.assertEqual(self.client.get(f"{URL}/{other.pk}").status_code, 404)
        self.assertEqual(self.client.post(f"{URL}/{other.pk}/cancel", {}, format="json").status_code, 404)
        self.assertEqual(self.client.get(URL, {"household": self.household_b.pk}).data["count"], 0)
        self.login_as(self.admin)
        self.assertEqual(self.client.get(URL).data["count"], 2)

    def test_filters(self):
        first = self.make_order()
        second = self.make_order(self.owner_b, kind=OrderKind.PRODUCT_ORDER)
        self.reject(second)
        self.login_as(self.warehouse)
        ids = lambda **params: [row["id"] for row in self.client.get(URL, params).data["results"]]  # noqa: E731
        self.assertEqual(ids(), [second.pk, first.pk])  # newest first
        self.assertEqual(ids(status="NEW,REJECTED"), [second.pk, first.pk])
        self.assertEqual(ids(status="REJECTED"), [second.pk])
        self.assertEqual(ids(kind="SUPPLY_REQUEST"), [first.pk])
        self.assertEqual(ids(household=self.household_b.pk), [second.pk])
        self.assertEqual(ids(search=first.code), [first.pk])
        self.assertEqual(ids(search=f"by-{first.pk}"), [first.pk])
        self.assertEqual(ids(search="Bog‘ishamol"), [second.pk])
        self.assertEqual(ids(search=self.household_a.code), [first.pk])
        today = timezone.localdate()
        self.assertEqual(len(ids(date_from=str(today), date_to=str(today))), 2)
        self.assertEqual(ids(date_from=str(today + timedelta(days=1))), [])
        response = self.client.get(URL, {"status": "NEW,BAD"})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.data, {"status": ["“BAD” ruxsat etilgan qiymat emas."]})
        self.assertEqual(self.client.get(URL, {"date_from": "2026-13-01"}).status_code, 400)
        list_row = self.client.get(URL).data["results"][0]
        self.assertNotIn("history", list_row)
        self.assertEqual(list_row["allowed_actions"], [])  # WAREHOUSE reads only

    def reject(self, order, reason="Ombor bo‘sh"):
        self.login_as(self.admin)
        response = self.client.post(f"{URL}/{order.pk}/reject", {"reason": reason}, format="json")
        self.assertEqual(response.status_code, 200, response.data)
        return response

    def test_query_budget_independent_of_size(self):
        def count_queries() -> int:
            self.login_as(self.admin)
            with CaptureQueriesContext(connection) as ctx:
                self.assertEqual(self.client.get(URL).status_code, 200)
            return len(ctx.captured_queries)

        self.make_order()
        small = count_queries()
        for _ in range(9):
            self.make_order(items=[{"product": self.feed.pk, "quantity": 5}, {"product": self.vitamin.pk, "quantity": 1}])
        self.assertEqual(count_queries(), small)
        self.assertLessEqual(small, 6)

        order = Order.objects.order_by("id").first()
        with CaptureQueriesContext(connection) as ctx:
            self.assertEqual(self.client.get(f"{URL}/{order.pk}").status_code, 200)
        self.assertLessEqual(len(ctx.captured_queries), 10)


class OrderDecisionTests(OrdersBase):
    def test_approve_without_task(self):
        order = self.make_order(items=[{"product": self.feed.pk, "quantity": 200},
                                       {"product": self.vitamin.pk, "quantity": 4}])
        feed_item, vitamin_item = order.items.order_by("id")
        self.login_as(self.admin)
        body = {"items": [{"id": feed_item.pk, "approved_qty": 150}, {"id": vitamin_item.pk, "approved_qty": 0}],
                "decision_note": "150 kg yetarli", "create_task": False}
        with self.captureOnCommitCallbacks(execute=True):
            response = self.client.post(f"{URL}/{order.pk}/approve", body, format="json")
        self.assertEqual(response.status_code, 200, response.data)
        data = response.data
        self.assertEqual((data["status"], data["status_display"]), ("APPROVED", "Tasdiqlandi"))
        self.assertEqual([i["approved_qty"] for i in data["items"]], [150.0, 0.0])
        self.assertEqual(data["decided_by"]["id"], self.admin.pk)
        self.assertEqual(data["decision_note"], "150 kg yetarli")
        self.assertIsNotNone(data["decided_at"])
        self.assertEqual([h["to_status"] for h in data["history"]], ["NEW", "APPROVED"])

        log = AuditLog.objects.get(module="orders", action="status_change")
        self.assertEqual(log.before, {"status": "NEW"})
        self.assertEqual((log.after["status"], log.after["note"]), ("APPROVED", "150 kg yetarli"))

        rows = list(Notification.objects.filter(event="order_approved"))
        self.assertEqual([n.recipient_id for n in rows], [self.owner_a.pk])
        self.assertEqual((rows[0].button_path, rows[0].status), (f"/my/orders/{order.pk}", "PENDING"))
        self.assertIn("Yem PK-1 — 150 kg", rows[0].text)
        self.assertNotIn("Vitamin", rows[0].text)

        self.assertEqual(self.approve(order).status_code, 409)
        self.assertEqual(self.approve(order).data["detail"], "Buyurtma holati: Tasdiqlandi. Amalni bajarib bo‘lmaydi.")

    def test_approve_validation(self):
        order = self.make_order(items=[{"product": self.feed.pk, "quantity": 200},
                                       {"product": self.vitamin.pk, "quantity": 4}])
        feed_item, vitamin_item = order.items.order_by("id")
        self.login_as(self.admin)
        cases = [
            ([{"id": feed_item.pk, "approved_qty": 10}],
             "Buyurtmadagi har bir mahsulot uchun miqdorni bir martadan ko‘rsating."),
            ([{"id": feed_item.pk, "approved_qty": 0}, {"id": vitamin_item.pk, "approved_qty": 0}],
             "Kamida bitta mahsulot miqdori 0 dan katta bo‘lsin."),
            ([{"id": feed_item.pk, "approved_qty": -1}, {"id": vitamin_item.pk, "approved_qty": 1}],
             "Tasdiqlangan miqdor manfiy bo‘lishi mumkin emas."),
            ([{"id": feed_item.pk, "approved_qty": 1}, {"id": feed_item.pk, "approved_qty": 1}],
             "Mahsulot takrorlanmasin."),
            ([{"id": feed_item.pk, "approved_qty": 1}, {"id": vitamin_item.pk, "approved_qty": "1.5"}],
             "“Vitamin C”: “dona” uchun butun son kiriting"),
        ]
        for items, message in cases:
            with self.subTest(message=message):
                response = self.client.post(f"{URL}/{order.pk}/approve", {"items": items, "create_task": False},
                                            format="json")
                self.assertEqual(response.status_code, 400, response.data)
                self.assertEqual(response.data, {"items": [message]})
        order.refresh_from_db()
        self.assertEqual(order.status, OrderStatus.NEW)
        self.assertFalse(OrderItem.objects.filter(approved_qty__isnull=False).exists())

    def test_approve_with_task_when_logistics_missing_saves_nothing(self):
        order = self.make_order()
        with no_logistics(), self.captureOnCommitCallbacks(execute=True):
            response = self.approve(order, create_task=True)
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.data["detail"], bridge.MSG_LOGISTICS_UNAVAILABLE)
        order.refresh_from_db()
        self.assertEqual(order.status, OrderStatus.NEW)
        self.assertFalse(AuditLog.objects.filter(action="status_change").exists())
        self.assertFalse(Notification.objects.filter(event="order_approved").exists())

    def test_only_admin_decides(self):
        order = self.make_order()
        for user in (self.owner_a, self.warehouse, self.logist):
            self.assertEqual(self.approve(order, user=user).status_code, 403)
            self.login_as(user)
            self.assertEqual(self.client.post(f"{URL}/{order.pk}/reject", {"reason": "yo‘q"},
                                              format="json").status_code, 403)
            self.assertEqual(self.client.post(f"{URL}/{order.pk}/create-task", {}, format="json").status_code, 403)

    def test_reject(self):
        order = self.make_order()
        self.login_as(self.admin)
        response = self.client.post(f"{URL}/{order.pk}/reject", {"reason": " a "}, format="json")
        self.assertEqual(response.data, {"reason": ["Sababni yozing (kamida 3 ta belgi)."]})
        with self.captureOnCommitCallbacks(execute=True):
            response = self.client.post(f"{URL}/{order.pk}/reject", {"reason": "Ombor bo‘sh"}, format="json")
        self.assertEqual(response.status_code, 200)
        self.assertEqual((response.data["status"], response.data["decision_note"]), ("REJECTED", "Ombor bo‘sh"))
        self.assertEqual(response.data["allowed_actions"], [])
        row = Notification.objects.get(event="order_rejected")
        self.assertEqual((row.recipient_id, row.button_path), (self.owner_a.pk, f"/my/orders/{order.pk}"))
        self.assertIn("Sabab: Ombor bo‘sh", row.text)
        again = self.client.post(f"{URL}/{order.pk}/reject", {"reason": "Ombor bo‘sh"}, format="json")
        self.assertEqual(again.status_code, 409)
        self.assertEqual(again.data["detail"], "Buyurtma holati: Rad etildi. Amalni bajarib bo‘lmaydi.")
        self.assertEqual(self.approve(order).status_code, 409)
        self.assertEqual(AuditLog.objects.filter(module="orders", action="status_change").count(), 1)


class OrderCancelTests(OrdersBase):
    def test_owner_cancels_new_without_reason(self):
        order = self.make_order()
        self.login_as(self.owner_a)
        with self.captureOnCommitCallbacks(execute=True):
            response = self.client.post(f"{URL}/{order.pk}/cancel", {}, format="json")
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["status"], "CANCELLED")
        order.refresh_from_db()
        self.assertEqual((order.cancelled_by_id, order.cancel_reason), (self.owner_a.pk, ""))
        self.assertIsNotNone(order.cancelled_at)
        rows = {n.recipient_id: n.button_path for n in Notification.objects.filter(event="order_cancelled")}
        self.assertEqual(rows, {self.admin.pk: f"/orders/{order.pk}", self.admin2.pk: f"/orders/{order.pk}"})
        self.assertEqual(self.client.post(f"{URL}/{order.pk}/cancel", {}, format="json").status_code, 409)

    def test_owner_cannot_cancel_approved(self):
        order = self.make_order()
        self.approve(order)
        self.login_as(self.owner_a)
        response = self.client.post(f"{URL}/{order.pk}/cancel", {"reason": "kerak emas"}, format="json")
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.data["detail"], "Buyurtma holati: Tasdiqlandi. Amalni bajarib bo‘lmaydi.")

    def test_owner_reason_limit(self):
        order = self.make_order()
        self.login_as(self.owner_a)
        response = self.client.post(f"{URL}/{order.pk}/cancel", {"reason": "x" * 501}, format="json")
        self.assertEqual(response.status_code, 400)
        self.assertIn("reason", response.data)

    def test_admin_cancels_approved_with_reason(self):
        order = self.make_order()
        self.approve(order)
        self.login_as(self.admin)
        response = self.client.post(f"{URL}/{order.pk}/cancel", {"reason": ""}, format="json")
        self.assertEqual(response.data, {"reason": ["Sababni yozing (kamida 3 ta belgi)."]})
        with self.captureOnCommitCallbacks(execute=True):
            response = self.client.post(f"{URL}/{order.pk}/cancel", {"reason": "Xonadon rad etdi"}, format="json")
        self.assertEqual(response.status_code, 200)
        self.assertEqual((response.data["status"], response.data["cancel_reason"]), ("CANCELLED", "Xonadon rad etdi"))
        rows = list(Notification.objects.filter(event="order_cancelled"))
        self.assertEqual([(n.recipient_id, n.button_path) for n in rows], [(self.owner_a.pk, f"/my/orders/{order.pk}")])
        self.assertEqual(self.client.post(f"{URL}/{order.pk}/cancel", {"reason": "yana bir"},
                                          format="json").status_code, 409)

    def test_warehouse_and_others_cannot_cancel(self):
        order = self.make_order()
        for user in (self.warehouse, self.logist, self.vet):
            self.login_as(user)
            self.assertEqual(self.client.post(f"{URL}/{order.pk}/cancel", {"reason": "abc"},
                                              format="json").status_code, 403)

    def test_admin_cannot_cancel_while_delivering(self):
        order = self.make_order()
        self.approve(order)
        Order.objects.filter(pk=order.pk).update(status=OrderStatus.IN_DELIVERY)
        self.login_as(self.admin)
        response = self.client.post(f"{URL}/{order.pk}/cancel", {"reason": "kech qoldi"}, format="json")
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.data["detail"],
                         "Yetkazib berish boshlangan. Avval logistika topshirig‘ini bekor qiling.")


class TaskSyncTests(OrdersBase):
    """Order status follows the linked logistics task (§6.2), tested with a stand-in task object."""

    def task(self, order, status, **extra):
        return SimpleNamespace(pk=77, code="LG-000077", order_id=order.pk, status=status, **extra)

    def test_full_cycle_and_idempotency(self):
        order = self.make_order()
        self.approve(order)
        for task_status, order_status in (("ASSIGNED", "APPROVED"), ("IN_TRANSIT", "IN_DELIVERY"),
                                          ("IN_TRANSIT", "IN_DELIVERY"), ("DELIVERED", "DELIVERED"),
                                          ("CONFIRMED", "CONFIRMED")):
            services.sync_order_with_task(task=self.task(order, task_status, received_ok=True), actor=self.logist)
            order.refresh_from_db()
            self.assertEqual(order.status, order_status, task_status)
        history = list(OrderStatusLog.objects.filter(order=order).values_list("to_status", "note"))
        self.assertEqual([h[0] for h in history], ["NEW", "APPROVED", "IN_DELIVERY", "DELIVERED", "CONFIRMED"])
        self.assertEqual(history[2][1], "LG-000077: Yo‘lda")

    def test_cancelled_task_returns_order_to_approved(self):
        order = self.make_order()
        self.approve(order)
        services.sync_order_with_task(task=self.task(order, "IN_TRANSIT"), actor=self.admin)
        services.sync_order_with_task(task=self.task(order, "CANCELLED", cancel_reason="Mashina buzildi"),
                                      actor=self.admin)
        order.refresh_from_db()
        self.assertEqual(order.status, OrderStatus.APPROVED)
        self.assertEqual(order.status_logs.last().note, "LG-000077: topshiriq bekor qilindi — Mashina buzildi")

    def test_problem_confirmation_note_and_impossible_jump(self):
        order = self.make_order()
        with self.assertRaises(services.Conflict):
            services.sync_order_with_task(task=self.task(order, "IN_TRANSIT"), actor=self.admin)
        services.sync_order_with_task(task=self.task(order, "CANCELLED"), actor=self.admin)  # NEW: no-op
        order.refresh_from_db()
        self.assertEqual(order.status, OrderStatus.NEW)
        self.approve(order)
        services.sync_order_with_task(
            task=self.task(order, "CONFIRMED", received_ok=False, issue_note="2 qop ho‘l"), actor=self.owner_a
        )
        order.refresh_from_db()
        self.assertEqual(order.status, OrderStatus.CONFIRMED)
        self.assertEqual(order.status_logs.last().note, "LG-000077: muammo bilan qabul qilindi — 2 qop ho‘l")

    def test_allowed_actions_follow_task(self):
        order = self.make_order()
        self.approve(order)
        order.refresh_from_db()
        assigned = SimpleNamespace(pk=5, status="ASSIGNED")
        self.assertEqual(selectors.allowed_actions(order, self.admin, assigned), ["cancel"])
        self.assertEqual(selectors.allowed_actions(order, self.admin, None, True), ["cancel", "create_task"])
        self.assertEqual(selectors.allowed_actions(order, self.admin, None, False), ["cancel"])
        self.assertEqual(selectors.allowed_actions(order, self.owner_a, assigned), [])
        order.status = OrderStatus.DELIVERED
        delivered = SimpleNamespace(pk=5, status="DELIVERED")
        self.assertEqual(selectors.allowed_actions(order, self.owner_a, delivered), ["confirm_receipt"])
        self.assertEqual(selectors.allowed_actions(order, self.admin, delivered), ["confirm_receipt"])
        self.assertEqual(selectors.allowed_actions(order, self.owner_b, delivered), [])
        self.assertEqual(selectors.allowed_actions(order, self.warehouse, delivered), [])

    def test_create_task_requires_approved_order(self):
        order = self.make_order()
        self.login_as(self.admin)
        response = self.client.post(f"{URL}/{order.pk}/create-task", {}, format="json")
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.data["detail"], "Buyurtma holati: Yangi. Amalni bajarib bo‘lmaydi.")
        self.approve(order)
        with no_logistics():
            self.login_as(self.admin)
            response = self.client.post(f"{URL}/{order.pk}/create-task", {}, format="json")
            self.assertEqual(response.status_code, 409)
            self.assertEqual(response.data["detail"], bridge.MSG_LOGISTICS_UNAVAILABLE)
            detail = self.client.get(f"{URL}/{order.pk}").data
            self.assertEqual(detail["allowed_actions"], ["cancel"])  # no create_task button without logistics


class CatalogTests(OrdersBase):
    def test_catalog_for_owner(self):
        Product.objects.filter(pk=self.vitamin.pk).update(reserved=Decimal("1000"))  # fully reserved
        Product.objects.filter(pk=make_product("Eski yem").pk).update(is_active=False)
        self.login_as(self.owner_a)
        response = self.client.get("/api/catalog/products")
        self.assertEqual(response.status_code, 200)
        rows = {row["name"]: row for row in response.data}
        self.assertNotIn("Eski yem", rows)
        self.assertEqual(set(rows["Yem PK-1"]), {"id", "name", "category", "category_display", "unit", "in_stock"})
        self.assertTrue(rows["Yem PK-1"]["in_stock"])
        self.assertFalse(rows["Vitamin C"]["in_stock"])
        self.assertFalse(rows["Vaksina ND"]["in_stock"])
        names = [row["name"] for row in response.data]
        self.assertLess(names.index("Yem PK-1"), names.index("Vaksina ND"))  # FEED before VACCINE
        self.assertEqual([r["name"] for r in self.client.get("/api/catalog/products", {"search": "vaks"}).data],
                         ["Vaksina ND"])
        self.assertEqual([r["name"] for r in self.client.get("/api/catalog/products",
                                                              {"category": "VACCINE"}).data], ["Vaksina ND"])
        self.assertEqual(self.client.get("/api/catalog/products", {"category": "X"}).status_code, 400)

    def test_catalog_roles(self):
        for user, expected in ((self.admin, 200), (self.warehouse, 200), (self.logist, 403), (self.vet, 403)):
            self.login_as(user)
            self.assertEqual(self.client.get("/api/catalog/products").status_code, expected)


class OrderReadModelsTests(OrdersBase):
    def test_dashboard_blocks(self):
        first = self.make_order()
        second = self.make_order(self.owner_b, items=[{"product": self.feed.pk, "quantity": 1},
                                                      {"product": self.vitamin.pk, "quantity": 1}])
        third = self.make_order()
        self.approve(third)
        block = selectors.admin_dashboard_block()
        self.assertEqual(block["orders"], {"new": 2, "approved_without_task": 1, "in_delivery": 0,
                                           "awaiting_confirmation": 0})
        self.assertEqual([row["id"] for row in block["pending_orders"]], [first.pk, second.pk])
        self.assertEqual(block["pending_orders"][1]["items_count"], 2)
        self.assertEqual(block["pending_orders"][1]["household"]["code"], self.household_b.code)
        Order.objects.filter(pk=third.pk).update(status=OrderStatus.DELIVERED)
        self.assertEqual(selectors.owner_counts(self.household_a), {"open": 2, "awaiting_confirmation": 1})
        self.assertEqual(selectors.owner_counts(self.household_b), {"open": 1, "awaiting_confirmation": 0})
        items, count = selectors.bot_new_orders()
        self.assertEqual((count, items[0]["code"]), (2, first.code))
        self.assertEqual(items[1]["items_text"], "Yem PK-1 — 1 kg; Vitamin C — 1 dona")
        self.assertEqual([o["id"] for o in selectors.owner_open_orders(self.household_a)], [third.pk, first.pk])

    def test_dashboard_endpoints(self):
        order = self.make_order()
        self.login_as(self.admin)
        data = self.client.get("/api/dashboard/admin").data
        self.assertEqual(data["orders"]["new"], 1)
        self.assertEqual(data["pending_orders"][0]["code"], order.code)
        self.assertRegex(data["pending_orders"][0]["created_at"], r"\+05:00$")
        self.login_as(self.owner_a)
        self.assertEqual(self.client.get("/api/dashboard/owner").data["orders"], {"open": 1, "awaiting_confirmation": 0})
        with mock.patch.object(selectors, "tables_ready", return_value=False):  # live DB before migrate
            self.assertEqual(self.client.get("/api/dashboard/owner").data["orders"],
                             {"open": 0, "awaiting_confirmation": 0})
            self.login_as(self.admin)
            data = self.client.get("/api/dashboard/admin").data
            self.assertEqual((data["orders"]["new"], data["pending_orders"]), (0, []))

    def test_global_search_group(self):
        own = self.make_order()
        other = self.make_order(self.owner_b)
        self.login_as(self.owner_a)
        groups = {g["type"]: g for g in self.client.get("/api/search", {"q": other.code}).data["groups"]}
        self.assertNotIn("order", groups)
        groups = {g["type"]: g for g in self.client.get("/api/search", {"q": own.code}).data["groups"]}
        self.assertEqual(groups["order"]["items"][0]["path"], f"/my/orders/{own.pk}")
        self.login_as(self.admin)
        groups = {g["type"]: g for g in self.client.get("/api/search", {"q": "Bog‘ishamol"}).data["groups"]}
        self.assertEqual([i["id"] for i in groups["order"]["items"]], [other.pk])
        self.assertEqual(groups["order"]["items"][0]["path"], f"/orders/{other.pk}")
        self.login_as(self.logist)
        groups = {g["type"] for g in self.client.get("/api/search", {"q": own.code}).data["groups"]}
        self.assertNotIn("order", groups)

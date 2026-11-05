"""Vehicles, drivers, bot endpoints and the order ↔ task sync (API_CONTRACT_V3 §6.2, §7.1, §7.2, §14.1)."""

from __future__ import annotations

from decimal import Decimal

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import override_settings

from audit.models import AuditLog, AuditModule
from common.roles import Role
from common.testing import BOT_API_TOKEN, make_user
from logistics.models import LogisticsTask, LogisticsTaskStatusLog, Vehicle
from warehouse.models import Product

from .helpers import PNG, LogisticsTestCase


class VehicleDriverTests(LogisticsTestCase):
    def test_vehicle_create_normalizes_and_rejects_duplicates(self):
        self.login_as(self.logist)
        response = self.client.post("/api/logistics/vehicles", {"plate_number": " 10  x 555 aa ", "model": "Labo",
                                                                 "capacity_kg": 800}, format="json")
        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(response.data["plate_number"], "10 X 555 AA")
        self.assertEqual(response.data["active_tasks"], 0)
        response = self.client.post("/api/logistics/vehicles", {"plate_number": "10 x 555 AA"}, format="json")
        self.assertEqual(response.data, {"plate_number": ["Bu davlat raqami allaqachon ro‘yxatda."]})
        response = self.client.post("/api/logistics/vehicles", {"plate_number": "АБ-1"}, format="json")
        self.assertEqual(response.status_code, 400)
        self.assertEqual(AuditLog.objects.filter(module=AuditModule.FLEET).count(), 1)

    def test_vehicle_patch_deactivate_and_active_tasks(self):
        self.create_task(10, vehicle=self.vehicle.pk, driver=self.driver.pk)
        self.login_as(self.admin)
        data = self.client.get(f"/api/logistics/vehicles/{self.vehicle.pk}").data
        self.assertEqual(data["active_tasks"], 1)
        response = self.client.patch(f"/api/logistics/vehicles/{self.vehicle.pk}", {"is_active": False}, format="json")
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.data["is_active"])
        self.assertEqual(self.client.get("/api/logistics/vehicles?is_active=1").data["count"], 1)
        self.assertEqual(self.client.delete(f"/api/logistics/vehicles/{self.vehicle.pk}").status_code, 405)

    def test_fleet_roles(self):
        for user, read, write in ((self.admin, 200, 201), (self.logist, 200, 201), (self.warehouse, 200, 403),
                                  (self.vet, 403, 403), (self.owner_a, 403, 403)):
            self.login_as(user)
            self.assertEqual(self.client.get("/api/logistics/drivers").status_code, read, user.username)
            response = self.client.post("/api/logistics/drivers", {"full_name": f"Haydovchi {user.pk}",
                                                                    "phone": "+998901234567"}, format="json")
            self.assertEqual(response.status_code, write, user.username)

    def test_driver_user_rules(self):
        self.login_as(self.admin)
        body = {"full_name": "Ali Karimov", "phone": "+998907776655"}
        response = self.client.post("/api/logistics/drivers", {**body, "user": self.vet.pk}, format="json")
        self.assertEqual(response.data, {"user": ["Faqat faol logistika xodimini bog‘lash mumkin."]})
        response = self.client.post("/api/logistics/drivers", {**body, "user": self.logist.pk}, format="json")
        self.assertEqual(response.data, {"user": ["Bu foydalanuvchi boshqa haydovchiga bog‘langan."]})
        response = self.client.post("/api/logistics/drivers", {**body, "user": self.logist2.pk}, format="json")
        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(response.data["user"]["username"], "logist2")
        self.assertTrue(response.data["user"]["telegram_linked"])
        response = self.client.post("/api/logistics/drivers", {"full_name": "Al", "phone": "abc"}, format="json")
        self.assertEqual(set(response.data), {"full_name", "phone"})


@override_settings(BOT_API_TOKEN=BOT_API_TOKEN)
class BotTests(LogisticsTestCase):
    def bot(self, method: str, url: str, data=None, **kwargs):
        return getattr(self.client, method)(url, data, HTTP_X_BOT_TOKEN=BOT_API_TOKEN, **kwargs)

    def test_logistics_task_list_and_actions(self):
        mine = self.create_task(10, vehicle=self.vehicle.pk, driver=self.driver.pk)["id"]
        unassigned = self.create_task(5)["id"]
        self.client.force_authenticate(None)
        data = self.bot("get", "/api/bot/users/7001/tasks").data
        self.assertEqual(data["role"], "LOGISTICS")
        self.assertEqual({row["id"] for row in data["tasks"]}, {mine, unassigned})
        row = next(row for row in data["tasks"] if row["id"] == mine)
        self.assertEqual(row["items_text"], "Yem PK-1 — 10 kg")
        self.assertEqual(row["vehicle_text"], "01 A 123 BC, Isuzu NPR")
        self.assertEqual(row["allowed_actions"], ["assign", "accept"])

        response = self.bot("post", f"/api/bot/tasks/{mine}/accept", {"telegram_id": 7001}, format="json")
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["status"], "ACCEPTED")
        self.assertEqual(LogisticsTaskStatusLog.objects.filter(task_id=mine).last().source, "BOT")
        response = self.bot("post", f"/api/bot/tasks/{mine}/accept", {"telegram_id": 7001}, format="json")
        self.assertEqual(response.status_code, 409)
        response = self.bot("post", f"/api/bot/tasks/{mine}/start", {"telegram_id": 7002}, format="json")
        self.assertEqual(response.status_code, 403)
        self.assertEqual(self.bot("post", f"/api/bot/tasks/{mine}/start", {"telegram_id": 7001},
                                  format="json").status_code, 200)

        upload = self.bot("post", "/api/bot/files", {"telegram_id": 7001, "purpose": "DELIVERY_PROOF",
                                                     "file": SimpleUploadedFile("foto.png", PNG)}, format="multipart")
        self.assertEqual(upload.status_code, 201, upload.data)
        response = self.bot("post", f"/api/bot/tasks/{mine}/deliver",
                            {"telegram_id": 7001, "file_ids": [upload.data["id"]]}, format="json")
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["status"], "DELIVERED")
        audit = AuditLog.objects.filter(module=AuditModule.LOGISTICS, object_id=mine).order_by("-id").first()
        self.assertEqual(audit.source, "BOT")

        self.owner_a.telegram_id = 9001
        self.owner_a.save()
        owner = self.bot("get", "/api/bot/users/9001/tasks").data
        self.assertEqual([row["id"] for row in owner["deliveries_to_confirm"]], [mine])
        response = self.bot("post", f"/api/bot/tasks/{mine}/confirm",
                            {"telegram_id": 9001, "received_ok": False, "issue_note": "Qop yirtilgan"}, format="json")
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["status"], "CONFIRMED")

    def test_unknown_or_blocked_user_and_other_roles(self):
        self.assertEqual(self.bot("get", "/api/bot/users/123456/tasks").status_code, 404)
        blocked = make_user("blocked_l", Role.LOGISTICS, telegram_id=7005, is_active=False)
        self.assertEqual(self.bot("get", f"/api/bot/users/{blocked.telegram_id}/tasks").status_code, 403)
        self.assertEqual(self.client.get("/api/bot/users/7001/tasks").status_code, 403)  # no bot token
        self.warehouse.telegram_id = 7100
        self.warehouse.save()
        self.create_task(10, vehicle=self.vehicle.pk, driver=self.driver.pk)
        data = self.bot("get", "/api/bot/users/7100/tasks").data
        self.assertEqual((data["role"], len(data["to_load"])), ("WAREHOUSE", 1))
        task_id = data["to_load"][0]["id"]
        response = self.bot("post", f"/api/bot/tasks/{task_id}/accept", {"telegram_id": 7100}, format="json")
        self.assertEqual(response.status_code, 403)
        self.admin.telegram_id = 7200
        self.admin.save()
        data = self.bot("get", "/api/bot/users/7200/tasks").data
        self.assertEqual(data["role"], "ADMIN")
        self.assertIn("deliveries", data)


class OrderSyncTests(LogisticsTestCase):
    def make_order(self, quantity: str = "120"):
        from orders import services as order_services

        return order_services.create_order(
            actor=self.owner_a,
            kind="SUPPLY_REQUEST",
            items=[order_services.ItemInput(product=self.feed, quantity=Decimal(quantity))],
        )

    def approve(self, order, quantity, **task):
        self.login_as(self.admin)
        item = order.items.get()
        return self.client.post(
            f"/api/orders/{order.pk}/approve",
            {"items": [{"id": item.pk, "approved_qty": quantity}], "create_task": True, "task": task},
            format="json",
        )

    def test_order_follows_task(self):
        order = self.make_order()
        response = self.approve(order, 100)
        self.assertEqual(response.status_code, 200, response.data)
        task = LogisticsTask.objects.get(order=order)
        self.assertEqual(task.items.get().order_item_id, order.items.get().pk)
        self.assertEqual(Product.objects.get(pk=self.feed.pk).reserved, Decimal("100"))

        self.login_as(self.admin)
        data = self.client.get(f"/api/logistics/tasks/{task.pk}").data
        self.assertEqual(data["order"], {"id": order.pk, "code": order.code})

        self.to_status(task.pk, "IN_TRANSIT")
        order.refresh_from_db()
        self.assertEqual(order.status, "IN_DELIVERY")
        self.to_status_delivered(task.pk)
        order.refresh_from_db()
        self.assertEqual(order.status, "DELIVERED")
        self.assertEqual(self.act(self.owner_a, task.pk, "confirm", {"received_ok": True}).status_code, 200)
        order.refresh_from_db()
        self.assertEqual(order.status, "CONFIRMED")

    def to_status_delivered(self, task_id):
        file_id = self.upload_proof(self.logist)
        self.assertEqual(self.act(self.logist, task_id, "deliver", {"file_ids": [file_id]}).status_code, 200)

    def test_cancelled_task_returns_order_to_approved_and_a_new_task_can_be_made(self):
        order = self.make_order()
        self.assertEqual(self.approve(order, 100).status_code, 200)
        task = LogisticsTask.objects.get(order=order)
        self.to_status(task.pk, "IN_TRANSIT")
        self.assertEqual(self.act(self.admin, task.pk, "cancel", {"reason": "Mashina buzildi"}).status_code, 200)
        order.refresh_from_db()
        self.assertEqual(order.status, "APPROVED")
        self.login_as(self.admin)
        response = self.client.post(f"/api/orders/{order.pk}/create-task", {}, format="json")
        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(LogisticsTask.objects.filter(order=order).count(), 2)

    def test_approve_with_short_stock_rolls_back(self):
        order = self.make_order("5000")
        response = self.approve(order, 5000)
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.data, {"items": ["“Yem PK-1”: omborda yetarli emas. Bo‘sh qoldiq: 1 000 kg"]})
        order.refresh_from_db()
        self.assertEqual(order.status, "NEW")
        self.assertFalse(LogisticsTask.objects.exists())

    def test_order_cancel_cancels_unstarted_task(self):
        order = self.make_order()
        self.assertEqual(self.approve(order, 100, vehicle=self.vehicle.pk, driver=self.driver.pk).status_code, 200)
        task = LogisticsTask.objects.get(order=order)
        self.assertEqual(task.status, "ASSIGNED")
        self.login_as(self.admin)
        response = self.client.post(f"/api/orders/{order.pk}/cancel", {"reason": "Mijoz voz kechdi"}, format="json")
        self.assertEqual(response.status_code, 200, response.data)
        task.refresh_from_db()
        self.assertEqual(task.status, "CANCELLED")
        self.assertEqual(Product.objects.get(pk=self.feed.pk).reserved, 0)


class VehicleModelTests(LogisticsTestCase):
    def test_plate_unique_case_insensitive_in_db(self):
        from django.db import IntegrityError, transaction

        with self.assertRaises(IntegrityError), transaction.atomic():
            Vehicle.objects.create(plate_number="01 a 123 bc")


@override_settings(BOT_API_TOKEN=BOT_API_TOKEN)
class BotSummaryTests(LogisticsTestCase):
    def test_logistics_summary_payload(self):
        task_id = self.create_task(10, vehicle=self.vehicle.pk, driver=self.driver.pk)["id"]
        self.to_status(task_id, "IN_TRANSIT")
        self.create_task(5)
        self.client.force_authenticate(None)
        data = self.client.get("/api/bot/users/7001/summary", HTTP_X_BOT_TOKEN=BOT_API_TOKEN).data
        self.assertEqual(data["role"], "LOGISTICS")
        self.assertEqual(
            data["today"],
            {"my_active": 1, "my_in_transit": 1, "new_unassigned": 1, "overdue": 0, "delivered_today": 0,
             "awaiting_confirmation": 0},
        )


class IntegrityTests(LogisticsTestCase):
    def test_integrity_problems(self):
        from logistics import integrity

        started = self.create_task(10)["id"]
        self.to_status(started, "IN_TRANSIT")
        self.create_task(20)
        self.assertEqual(integrity.problems(), [])
        Product.objects.filter(pk=self.feed.pk).update(reserved=Decimal("5"))
        self.assertEqual(len(integrity.problems()), 1)

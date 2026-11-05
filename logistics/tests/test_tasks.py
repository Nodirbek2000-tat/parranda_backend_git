"""Logistics tasks (API_CONTRACT_V3 §7, §19.1): status machine, roles, stock, files, audit, notifications."""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from audit.models import AuditAction, AuditLog, AuditModule
from core.models import Attachment
from core.settings_service import get_settings
from logistics.models import LogisticsTask, LogisticsTaskItem, TaskStatus
from notifications.models import Notification
from warehouse.models import Product, StockTransaction, StockTransactionType

from common.testing import make_product
from warehouse import services as warehouse_services
from warehouse.models import Unit

from .helpers import LogisticsTestCase


def product(pk: int) -> Product:
    return Product.objects.get(pk=pk)


class HappyPathTests(LogisticsTestCase):
    def test_full_path_new_to_confirmed(self):
        data = self.create_task(150, planned_date=str(timezone.localdate() + timedelta(days=2)))
        task_id = data["id"]
        self.assertEqual(data["status"], "NEW")
        self.assertEqual(data["code"], f"LG-{task_id:06d}")
        self.assertEqual(data["delivery"]["address"], "Samarqand viloyati, Navobod tumani, Navbahor MFY, 12-uy")
        self.assertEqual(data["total_kg"], 150.0)
        self.assertEqual(data["allowed_actions"], ["assign", "cancel", "edit"])
        self.assertEqual(product(self.feed.pk).reserved, Decimal("150"))

        response = self.act(self.admin, task_id, "assign", {"vehicle": self.vehicle.pk, "driver": self.driver.pk})
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["status"], "ASSIGNED")
        self.assertEqual(response.data["responsible"]["id"], self.logist.pk)  # the driver's linked user

        self.login_as(self.logist)
        self.assertEqual(
            self.client.get(f"/api/logistics/tasks/{task_id}").data["allowed_actions"], ["assign", "accept"]
        )
        self.assertEqual(self.act(self.logist, task_id, "accept").status_code, 200)

        balance_before = product(self.feed.pk).balance
        response = self.act(self.logist, task_id, "start", {"latitude": 39.65, "longitude": 66.96})
        self.assertEqual(response.status_code, 200, response.data)
        feed = product(self.feed.pk)
        self.assertEqual(feed.balance, balance_before - 150)
        self.assertEqual(feed.reserved, 0)
        out = StockTransaction.objects.get(type=StockTransactionType.OUT, reference_type="logistics_task")
        self.assertEqual(out.reference_id, task_id)
        self.assertEqual(out.household_id, self.household_a.pk)
        self.assertEqual(out.document_no, f"LG-{task_id:06d}")
        self.assertEqual(response.data["items"][0]["stock_transaction"], {"id": out.pk, "op_id": out.op_id})
        self.assertEqual(response.data["history"][-1]["latitude"], 39.65)

        file_id = self.upload_proof(self.logist)
        response = self.act(self.logist, task_id, "deliver", {"file_ids": [file_id], "recipient_name": "Bobur"})
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["status"], "DELIVERED")
        self.assertEqual([row["id"] for row in response.data["proof_files"]], [file_id])
        self.assertEqual(Attachment.objects.get(pk=file_id).household_id, self.household_a.pk)

        self.login_as(self.owner_a)
        owner_view = self.client.get(f"/api/logistics/tasks/{task_id}").data
        self.assertEqual(owner_view["allowed_actions"], ["confirm"])
        self.assertNotIn("responsible", owner_view)
        self.assertNotIn("created_by", owner_view)
        self.assertEqual({row["actor"]["full_name"] for row in owner_view["history"]}, {"NNP"})
        response = self.act(self.owner_a, task_id, "confirm", {"received_ok": True})
        self.assertEqual(response.status_code, 200, response.data)

        task = LogisticsTask.objects.get(pk=task_id)
        self.assertEqual(task.status, TaskStatus.CONFIRMED)
        for field in ("assigned_at", "accepted_at", "started_at", "delivered_at", "confirmed_at"):
            self.assertIsNotNone(getattr(task, field), field)
        self.assertEqual(
            list(task.history.values_list("to_status", flat=True)),
            ["NEW", "ASSIGNED", "ACCEPTED", "IN_TRANSIT", "DELIVERED", "CONFIRMED"],
        )
        audits = AuditLog.objects.filter(module=AuditModule.LOGISTICS, object_id=task_id)
        self.assertEqual(audits.filter(action=AuditAction.CREATE).count(), 1)
        self.assertEqual(audits.filter(action=AuditAction.STATUS_CHANGE).count(), 5)
        self.assertEqual(AuditLog.objects.filter(module=AuditModule.STOCK, object_id=out.pk).count(), 1)

    def test_create_with_vehicle_and_driver_is_assigned(self):
        data = self.create_task(100, vehicle=self.vehicle.pk, driver=self.driver.pk)
        self.assertEqual(data["status"], "ASSIGNED")
        self.assertEqual(LogisticsTask.objects.get(pk=data["id"]).assign_seq, 1)

    def test_admin_confirms_for_owner_with_reason_and_problem_confirm(self):
        task_id = self.create_task(10)["id"]
        self.to_status(task_id, "DELIVERED")
        response = self.act(self.admin, task_id, "confirm", {"received_ok": True})
        self.assertEqual(response.data, {"issue_note": ["Egasi nomidan tasdiqlash sababini yozing."]})
        response = self.act(self.owner_a, task_id, "confirm", {"received_ok": False})
        self.assertEqual(response.data, {"issue_note": ["Muammoni qisqacha yozing."]})
        response = self.act(self.owner_a, task_id, "confirm", {"received_ok": False, "issue_note": "2 qop yirtiq"})
        self.assertEqual(response.status_code, 200, response.data)
        self.assertIs(response.data["received_ok"], False)


class TransitionRuleTests(LogisticsTestCase):
    def test_illegal_transitions_are_409(self):
        task_id = self.create_task(10)["id"]
        cases = [
            (self.logist, "accept", {}),
            (self.logist, "start", {}),
            (self.logist, "deliver", {}),
            (self.owner_a, "confirm", {"received_ok": True}),
        ]
        for user, action, body in cases:
            response = self.act(user, task_id, action, body)
            self.assertEqual(response.status_code, 409, (action, response.data))
            self.assertEqual(response.data["detail"], "Topshiriq holati: Yangi. Bu amalni bajarib bo‘lmaydi.")
        self.to_status(task_id, "IN_TRANSIT")
        self.assertEqual(self.act(self.logist, task_id, "accept").status_code, 409)
        self.assertEqual(
            self.act(self.admin, task_id, "assign", {"vehicle": self.vehicle.pk, "driver": self.driver.pk}).status_code,
            409,
        )
        self.login_as(self.admin)
        self.assertEqual(self.client.patch(f"/api/logistics/tasks/{task_id}", {"note": "x"}, format="json").status_code,
                         409)
        self.to_status_from_transit(task_id)
        self.assertEqual(self.act(self.admin, task_id, "cancel", {"reason": "Kerak emas"}).status_code, 409)

    def to_status_from_transit(self, task_id):
        file_id = self.upload_proof(self.logist)
        self.assertEqual(self.act(self.logist, task_id, "deliver", {"file_ids": [file_id]}).status_code, 200)
        self.assertEqual(self.act(self.owner_a, task_id, "confirm", {"received_ok": True}).status_code, 200)

    def test_replayed_start_is_409_and_posts_one_out(self):
        task_id = self.create_task(100)["id"]
        self.to_status(task_id, "IN_TRANSIT")
        response = self.act(self.logist, task_id, "start")
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.data["detail"], "Topshiriq holati: Yo‘lda. Bu amalni bajarib bo‘lmaydi.")
        self.assertEqual(StockTransaction.objects.filter(reference_id=task_id, type="OUT").count(), 1)

    def test_other_logistics_worker_gets_403(self):
        task_id = self.create_task(10, vehicle=self.vehicle.pk, driver=self.driver.pk)["id"]
        response = self.act(self.logist2, task_id, "accept")
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.data["detail"], "Bu topshiriq boshqa xodimga biriktirilgan.")
        self.assertEqual(self.act(self.logist, task_id, "accept").status_code, 200)
        self.assertEqual(self.act(self.logist2, task_id, "start").status_code, 403)

    def test_accepting_logistics_becomes_responsible_when_nobody_is(self):
        task_id = self.create_task(10, vehicle=self.vehicle.pk, driver=self.driver2.pk)["id"]  # unlinked driver
        self.assertIsNone(LogisticsTask.objects.get(pk=task_id).responsible_id)
        self.assertEqual(self.act(self.logist2, task_id, "accept").status_code, 200)
        self.assertEqual(LogisticsTask.objects.get(pk=task_id).responsible_id, self.logist2.pk)
        self.assertEqual(self.act(self.logist, task_id, "start").status_code, 403)
        self.assertEqual(self.act(self.logist2, task_id, "start").status_code, 200)

    def test_role_matrix(self):
        task_id = self.create_task(10)["id"]
        url = f"/api/logistics/tasks/{task_id}"
        for user, expected in ((self.admin, 200), (self.warehouse, 200), (self.logist, 200), (self.owner_a, 200),
                               (self.owner_b, 404), (self.vet, 403)):
            self.login_as(user)
            self.assertEqual(self.client.get(url).status_code, expected, user.username)
        self.login_as(self.owner_b)
        self.assertEqual(self.client.get("/api/logistics/tasks").data["count"], 0)
        self.login_as(self.owner_a)
        self.assertEqual(self.client.get("/api/logistics/tasks").data["count"], 1)
        # writes
        self.assertEqual(self.act(self.warehouse, task_id, "assign",
                                  {"vehicle": self.vehicle.pk, "driver": self.driver.pk}).status_code, 403)
        self.assertEqual(self.act(self.logist, task_id, "cancel", {"reason": "Sabab"}).status_code, 403)
        self.assertEqual(self.act(self.owner_b, task_id, "confirm", {"received_ok": True}).status_code, 404)
        self.login_as(self.logist)
        body = {"household": self.household_a.pk, "items": [{"product": self.feed.pk, "quantity": 1}]}
        self.assertEqual(self.client.post("/api/logistics/tasks", body, format="json").status_code, 403)
        self.assertEqual(self.client.delete(url).status_code, 405)

    def test_owner_cannot_confirm_other_household_task(self):
        task_id = self.create_task(10)["id"]
        self.to_status(task_id, "DELIVERED")
        self.login_as(self.owner_b)
        # confirm needs the OWNER role (passes) and then the household scope → 404.
        response = self.client.post(f"/api/logistics/tasks/{task_id}/confirm", {"received_ok": True}, format="json")
        self.assertEqual(response.status_code, 404)


class ValidationTests(LogisticsTestCase):
    def test_create_validation_messages(self):
        self.login_as(self.admin)
        url = "/api/logistics/tasks"
        base = {"household": self.household_a.pk}
        cases = [
            ({"items": []}, {"items": ["Kamida bitta mahsulot tanlang."]}),
            ({"items": [{"product": self.feed.pk, "quantity": 1}, {"product": self.feed.pk, "quantity": 2}]},
             {"items": ["Mahsulot takrorlanmasin."]}),
            ({"items": [{"product": self.vitamin.pk, "quantity": "1.5"}]},
             {"items": ["“Vitamin”: “dona” uchun butun son kiriting"]}),
            ({"items": [{"product": self.feed.pk, "quantity": 2000}]},
             {"items": ["“Yem PK-1”: omborda yetarli emas. Bo‘sh qoldiq: 1 000 kg"]}),
            ({"items": [{"product": self.feed.pk, "quantity": 1}],
              "planned_date": str(timezone.localdate() - timedelta(days=1))},
             {"planned_date": ["Sana o‘tgan kun bo‘lishi mumkin emas."]}),
            ({"items": [{"product": self.feed.pk, "quantity": 1}], "vehicle": self.vehicle.pk},
             {"driver": ["Transport va haydovchini birga tanlang."]}),
        ]
        for extra, expected in cases:
            response = self.client.post(url, {**base, **extra}, format="json")
            self.assertEqual(response.status_code, 400, extra)
            self.assertEqual(response.data, expected)
        self.household_b.is_active = False
        self.household_b.save()
        response = self.client.post(url, {"household": self.household_b.pk,
                                          "items": [{"product": self.feed.pk, "quantity": 1}]}, format="json")
        self.assertEqual(response.data, {"household": ["Xonadon faol emas"]})
        self.assertFalse(LogisticsTask.objects.exists())
        self.assertEqual(product(self.feed.pk).reserved, 0)

    def test_reservation_blocks_second_task_and_manual_out(self):
        self.create_task(900)
        self.login_as(self.admin)
        response = self.client.post("/api/logistics/tasks", {
            "household": self.household_b.pk, "items": [{"product": self.feed.pk, "quantity": 200}]}, format="json")
        self.assertEqual(response.data, {"items": ["“Yem PK-1”: omborda yetarli emas. Bo‘sh qoldiq: 100 kg"]})
        response = self.client.post("/api/stock-transactions", {
            "product": self.feed.pk, "type": "OUT", "quantity": 150, "document_no": "CH-1"}, format="json")
        self.assertEqual(response.status_code, 400)
        self.assertEqual(
            response.data["quantity"],
            ["Omborda yetarli emas. Bo‘sh qoldiq: 100 kg (900 kg yetkazib berish uchun zaxiralangan)"],
        )

    def test_capacity_and_inactive_staff(self):
        task_id = self.create_task(150)["id"]
        response = self.act(self.admin, task_id, "assign", {"vehicle": self.small_vehicle.pk, "driver": self.driver.pk})
        self.assertEqual(response.data, {"vehicle": ["Yuk transport sig‘imidan oshadi: 150 kg > 100 kg."]})
        self.driver2.is_active = False
        self.driver2.save()
        response = self.act(self.admin, task_id, "assign", {"vehicle": self.vehicle.pk, "driver": self.driver2.pk})
        self.assertEqual(response.data, {"driver": ["Haydovchi faol emas."]})
        response = self.act(self.admin, task_id, "assign",
                            {"vehicle": self.vehicle.pk, "driver": self.driver.pk, "responsible": self.vet.pk})
        self.assertEqual(response.data, {"responsible": ["Mas’ul faqat faol logistika xodimi bo‘lishi mumkin."]})

    def test_proof_required_by_setting(self):
        task_id = self.create_task(10)["id"]
        self.to_status(task_id, "IN_TRANSIT")
        response = self.act(self.logist, task_id, "deliver", {})
        self.assertEqual(response.data, {"file_ids": ["Yetkazib berish dalilini (foto yoki imzo) biriktiring."]})
        settings_row = get_settings()
        settings_row.delivery_proof_required = False
        settings_row.save()
        self.assertEqual(self.act(self.logist, task_id, "deliver", {}).status_code, 200)

    def test_patch_only_planned_date_and_note(self):
        task_id = self.create_task(10)["id"]
        self.login_as(self.admin)
        tomorrow = str(timezone.localdate() + timedelta(days=1))
        response = self.client.patch(f"/api/logistics/tasks/{task_id}", {"planned_date": tomorrow, "note": "Ertalab"},
                                     format="json")
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual((response.data["planned_date"], response.data["note"]), (tomorrow, "Ertalab"))
        self.assertEqual(
            AuditLog.objects.filter(module=AuditModule.LOGISTICS, action=AuditAction.UPDATE).count(), 1
        )


class CancelTests(LogisticsTestCase):
    def test_cancel_before_start_releases_reservation(self):
        task_id = self.create_task(200)["id"]
        self.to_status(task_id, "ACCEPTED")
        response = self.act(self.admin, task_id, "cancel", {"reason": "x"})
        self.assertEqual(response.data, {"reason": ["Sababni yozing (kamida 3 ta belgi)."]})
        response = self.act(self.admin, task_id, "cancel", {"reason": "Mijoz rad etdi"})
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(product(self.feed.pk).reserved, 0)
        self.assertEqual(LogisticsTaskItem.objects.get(task_id=task_id).reserved_qty, 0)
        self.assertFalse(StockTransaction.objects.filter(reference_id=task_id).exists())

    def test_cancel_in_transit_returns_goods_once(self):
        task_id = self.create_task(200)["id"]
        self.to_status(task_id, "IN_TRANSIT")
        balance = product(self.feed.pk).balance
        self.assertEqual(self.act(self.admin, task_id, "cancel", {"reason": "Yo‘l yopiq"}).status_code, 200)
        self.assertEqual(product(self.feed.pk).balance, balance + 200)
        returned = StockTransaction.objects.filter(reference_id=task_id, type=StockTransactionType.IN)
        self.assertEqual(returned.count(), 1)
        self.assertEqual(LogisticsTaskItem.objects.get(task_id=task_id).return_transaction_id, returned.get().pk)
        self.assertEqual(self.act(self.admin, task_id, "cancel", {"reason": "Yana bir marta"}).status_code, 409)
        self.assertEqual(returned.count(), 1)


class NotificationTests(LogisticsTestCase):
    def rows(self, event):
        return Notification.objects.filter(event=event)

    def test_events_and_recipients(self):
        with self.captureOnCommitCallbacks(execute=True):
            task_id = self.create_task(10)["id"]
        self.assertEqual(set(self.rows("task_created").values_list("recipient_id", flat=True)),
                         {self.logist.pk, self.logist2.pk})
        with self.captureOnCommitCallbacks(execute=True):
            self.act(self.admin, task_id, "assign", {"vehicle": self.vehicle.pk, "driver": self.driver.pk})
        first = self.rows("driver_assigned")
        self.assertEqual(set(first.values_list("recipient_id", flat=True)), {self.logist.pk})
        self.assertEqual(first.get().button_path, f"/logistics/{task_id}")
        with self.captureOnCommitCallbacks(execute=True):  # re-assign → new key, one more row
            self.act(self.admin, task_id, "assign", {"vehicle": self.vehicle.pk, "driver": self.driver.pk})
        self.assertEqual(self.rows("driver_assigned").count(), 2)

        with self.captureOnCommitCallbacks(execute=True):
            self.act(self.logist, task_id, "accept")
            self.act(self.logist, task_id, "start")
        started = self.rows("delivery_started").get()
        self.assertEqual((started.recipient_id, started.button_path), (self.owner_a.pk, "/my/deliveries"))
        # task OUT → stock_out for WAREHOUSE only (never the owner)
        self.assertFalse(self.rows("stock_out").filter(recipient=self.owner_a).exists())
        self.assertTrue(self.rows("stock_out").filter(recipient=self.warehouse).exists())

        file_id = self.upload_proof(self.logist)
        with self.captureOnCommitCallbacks(execute=True):
            self.act(self.logist, task_id, "deliver", {"file_ids": [file_id]})
        self.assertEqual(set(self.rows("delivery_delivered").values_list("recipient_id", flat=True)),
                         {self.admin.pk, self.owner_a.pk})
        with self.captureOnCommitCallbacks(execute=True):
            self.act(self.owner_a, task_id, "confirm", {"received_ok": False, "issue_note": "Kam keldi"})
        confirmed = self.rows("delivery_confirmed")
        self.assertEqual(set(confirmed.values_list("recipient_id", flat=True)), {self.admin.pk, self.logist.pk})
        self.assertIn("Muammo bilan qabul qilindi", confirmed.first().text)

    def test_cancel_notifies_owner_only_after_assignment(self):
        new_id = self.create_task(10)["id"]
        with self.captureOnCommitCallbacks(execute=True):
            self.act(self.admin, new_id, "cancel", {"reason": "Kerak emas"})
        self.assertFalse(self.rows("task_cancelled").exists())
        assigned_id = self.create_task(10, vehicle=self.vehicle.pk, driver=self.driver.pk)["id"]
        with self.captureOnCommitCallbacks(execute=True):
            self.act(self.admin, assigned_id, "cancel", {"reason": "Kerak emas"})
        self.assertEqual(set(self.rows("task_cancelled").values_list("recipient_id", flat=True)),
                         {self.logist.pk, self.owner_a.pk})


class ListAndSummaryTests(LogisticsTestCase):
    def test_filters_and_order(self):
        today = timezone.localdate()
        late = self.create_task(10, planned_date=str(today))["id"]
        LogisticsTask.objects.filter(pk=late).update(planned_date=today - timedelta(days=2))
        later = self.create_task(10, planned_date=str(today + timedelta(days=5)))["id"]
        no_date = self.create_task(10, vehicle=self.vehicle.pk, driver=self.driver.pk)["id"]
        self.login_as(self.logist)
        ids = [row["id"] for row in self.client.get("/api/logistics/tasks").data["results"]]
        self.assertEqual(ids, [late, later, no_date])
        data = self.client.get("/api/logistics/tasks?overdue=1").data
        self.assertEqual([row["id"] for row in data["results"]], [late])
        self.assertTrue(data["results"][0]["is_overdue"])
        self.assertEqual([r["id"] for r in self.client.get("/api/logistics/tasks?mine=1").data["results"]], [no_date])
        self.assertEqual(self.client.get("/api/logistics/tasks?status=NEW,ASSIGNED").data["count"], 3)
        self.assertEqual(self.client.get("/api/logistics/tasks?status=BAD").status_code, 400)
        self.assertEqual(self.client.get(f"/api/logistics/tasks?search=LG-{later}").data["count"], 1)
        self.assertEqual(self.client.get("/api/logistics/tasks?responsible=me").data["count"], 1)

    def test_list_query_budget_is_constant(self):
        def count_queries():
            self.login_as(self.admin)
            with CaptureQueriesContext(connection) as ctx:
                self.assertEqual(self.client.get("/api/logistics/tasks").status_code, 200)
            return len(ctx.captured_queries)

        self.create_task(5, vehicle=self.vehicle.pk, driver=self.driver.pk)
        one = count_queries()
        for _ in range(4):
            self.create_task(5, vehicle=self.vehicle.pk, driver=self.driver.pk)
        self.assertEqual(count_queries(), one)
        self.assertLessEqual(one, 6)

    def test_summary(self):
        task_id = self.create_task(10, planned_date=str(timezone.localdate()))["id"]
        self.to_status(task_id, "DELIVERED")
        self.create_task(10)
        self.login_as(self.logist)
        with CaptureQueriesContext(connection) as ctx:
            data = self.client.get("/api/logistics/summary").data
        self.assertLessEqual(len(ctx.captured_queries), 6)
        self.assertEqual(data["by_status"], {"NEW": 1, "ASSIGNED": 0, "ACCEPTED": 0, "IN_TRANSIT": 0, "DELIVERED": 1})
        self.assertEqual(data["delivered_today"], 1)
        self.assertEqual(data["last_30d"]["on_time_pct"], 100.0)
        self.assertEqual(data["last_30d"]["created"], 2)
        for user, expected in ((self.owner_a, 403), (self.vet, 403), (self.warehouse, 200)):
            self.login_as(user)
            self.assertEqual(self.client.get("/api/logistics/summary").status_code, expected)

    def test_global_search_has_task_group(self):
        task_id = self.create_task(10)["id"]
        self.login_as(self.logist)
        groups = self.client.get(f"/api/search?q=LG-{task_id:06d}").data["groups"]
        self.assertEqual([g["type"] for g in groups], ["task"])
        self.assertEqual(groups[0]["items"][0]["path"], f"/logistics/{task_id}")
        self.login_as(self.owner_b)
        self.assertEqual(self.client.get(f"/api/search?q=LG-{task_id:06d}").data["groups"], [])


class ProofVisibilityTests(LogisticsTestCase):
    def test_proof_file_visible_to_owner_and_staff_only(self):
        task_id = self.create_task(10)["id"]
        self.to_status(task_id, "DELIVERED")
        attachment = Attachment.objects.get(object_type="LogisticsTask", object_id=task_id)
        for user, expected in ((self.owner_a, 200), (self.warehouse, 200), (self.owner_b, 404), (self.vet, 404)):
            self.login_as(user)
            self.assertEqual(self.client.get(f"/api/files/{attachment.pk}").status_code, expected, user.username)


class InTransitCancelFindingsTests(LogisticsTestCase):
    """Cancel in transit: returned goods keep lot/expiry, and the OUT is not 'issued to the household'."""

    def test_return_keeps_lot_and_expiry_and_household_consumption_excludes_it(self):
        from datetime import timedelta

        from django.utils import timezone

        from warehouse.models import Product, StockLot

        expiry = timezone.localdate() + timedelta(days=15)
        vaccine = make_product("Vaksina ND", min_stock="0", unit=Unit.PIECE)
        warehouse_services.record_stock_transaction(
            actor=self.admin, product_id=vaccine.pk, transaction_type=StockTransactionType.IN,
            quantity=Decimal("300"), document_no="KR-2", lot_no="L-11", expiry_date=expiry,
        )
        warehouse_services.record_stock_transaction(
            actor=self.admin, product_id=vaccine.pk, transaction_type=StockTransactionType.IN,
            quantity=Decimal("300"), document_no="KR-3", lot_no="L-12", expiry_date=expiry + timedelta(days=200),
        )
        self.login_as(self.admin)
        response = self.client.post("/api/logistics/tasks", {
            "household": self.household_a.pk,
            "items": [{"product": vaccine.pk, "quantity": 300}, {"product": self.feed.pk, "quantity": 500}],
        }, format="json")
        self.assertEqual(response.status_code, 201, response.data)
        task_id = response.data["id"]
        self.to_status(task_id, "IN_TRANSIT")
        self.assertEqual(StockLot.objects.get(lot_no="L-11").remaining_qty, 0)  # FEFO took the near lot
        self.assertEqual(self.act(self.admin, task_id, "cancel", {"reason": "Yo‘l yopiq"}).status_code, 200)

        returned = StockLot.objects.get(in_transaction__reference_id=task_id, product=vaccine)
        self.assertEqual((returned.lot_no, returned.expiry_date, returned.remaining_qty), ("L-11", expiry, 300))
        self.assertEqual(Product.objects.filter(pk=vaccine.pk).values_list("balance", flat=True).get(), 600)

        # Warehouse stats: no household is credited with the 500 kg that came back.
        self.login_as(self.admin)
        stats = self.client.get("/api/warehouse/stats").data
        self.assertEqual(stats["top_consumers"], [])
        # Feed report: nothing issued to the household.
        report = self.client.get("/api/reports/feed_consumption?format=json").data
        self.assertTrue(all(not row.get("feed_issued_kg") for row in report["rows"]), report["rows"])
        self.assertFalse(report["totals"].get("feed_issued_kg"))
        # The owner's own history does not show the delivery he never received.
        self.login_as(self.owner_a)
        rows = self.client.get("/api/stock-transactions").data["results"]
        self.assertFalse([row for row in rows if row["reference"] and row["reference"]["id"] == task_id])


class PunctualityOracleTests(LogisticsTestCase):
    """/api/logistics/summary and the logistics_efficiency report use one on-time definition."""

    def test_summary_and_report_agree(self):
        from datetime import datetime, time

        today = timezone.localdate()
        delivered_ids = []
        for index in range(4):
            task_id = self.create_task(10)["id"]
            self.to_status(task_id, "CONFIRMED" if index % 2 else "DELIVERED")
            delivered_ids.append(task_id)
        # Two on time, one late, one without a plan date.
        delivered_at = timezone.make_aware(datetime.combine(today - timedelta(days=2), time(12, 0)))
        plans = [today - timedelta(days=2), today, today - timedelta(days=3), None]
        for task_id, plan in zip(delivered_ids, plans):
            LogisticsTask.objects.filter(pk=task_id).update(planned_date=plan, delivered_at=delivered_at)
        # An open overdue task: not judged on time, counted separately.
        open_id = self.create_task(10)["id"]
        LogisticsTask.objects.filter(pk=open_id).update(planned_date=today - timedelta(days=1))
        LogisticsTask.objects.filter(pk=delivered_ids[0]).update(received_ok=True, issue_note="Egasi telefonda tasdiqladi")

        self.login_as(self.admin)
        summary = self.client.get("/api/logistics/summary").data["last_30d"]
        self.assertEqual(summary["on_time_pct"], 66.7)
        report = self.client.get("/api/reports/logistics_efficiency?format=json").data
        values = {row["label"]: row["value"] for row in report["summary"]}
        self.assertEqual(values["O‘z vaqtida"], f"{summary['on_time_pct']:.1f}%")
        self.assertEqual(values["Yetkazilgan"], str(summary["delivered"]))
        self.assertEqual(values["Muddati o‘tgan (ochiq)"], "1")
        rows = {row["code"]: row for row in report["rows"]}
        open_code = LogisticsTask.objects.get(pk=open_id).code
        self.assertIsNone(rows[open_code]["on_time"])
        first_code = LogisticsTask.objects.get(pk=delivered_ids[0]).code
        self.assertEqual(rows[first_code]["issue"], "")  # received OK → not a problem


class InactiveContextualFallbackTests(LogisticsTestCase):
    def test_driver_assigned_falls_back_to_logistics_when_driver_user_inactive(self):
        from accounts.models import User

        task_id = self.create_task(10)["id"]
        User.objects.filter(pk=self.logist.pk).update(is_active=False)
        with self.captureOnCommitCallbacks(execute=True):
            response = self.act(self.admin, task_id, "assign", {"vehicle": self.vehicle.pk, "driver": self.driver.pk})
        self.assertIn(response.status_code, (200, 400), response.data)
        if response.status_code == 200:
            recipients = set(Notification.objects.filter(event="driver_assigned").values_list("recipient_id", flat=True))
            self.assertEqual(recipients, {self.logist2.pk})

    def test_notify_drops_inactive_contextual_before_fallback(self):
        from accounts.models import User
        from notifications import services as notify_services
        from notifications import registry

        User.objects.filter(pk=self.logist.pk).update(is_active=False)
        rows = notify_services.notify(
            "driver_assigned", key="t1", text="x", staff_path="/logistics/1",
            contextual={registry.DRIVER_USER: self.logist, registry.RESPONSIBLE: None},
        )
        self.assertEqual({row.recipient_id for row in rows}, {self.logist2.pk})

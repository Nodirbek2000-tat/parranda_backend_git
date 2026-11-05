"""Verifier's tests (API_CONTRACT_V3 §7, §9.4, §14.1, §17.3): isolation order, no side effects on rejected
actions, re-assignment, notification texts, query budgets, integrity after the full flows."""

from __future__ import annotations

from datetime import timedelta

from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import connection
from django.test import override_settings
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from audit.models import AuditLog, AuditModule
from common.testing import BOT_API_TOKEN
from logistics import integrity, selectors
from logistics.models import LogisticsTask, LogisticsTaskStatusLog, TaskStatus
from notifications.models import Notification
from warehouse.models import Product, StockTransaction

from .helpers import PNG, LogisticsTestCase


class IsolationOrderTests(LogisticsTestCase):
    def test_other_owner_gets_404_before_body_validation(self):
        task_id = self.create_task(10)["id"]
        self.to_status(task_id, "DELIVERED")
        for body in ({}, {"received_ok": False}, {"received_ok": False, "issue_note": ""}):
            response = self.act(self.owner_b, task_id, "confirm", body)
            self.assertEqual(response.status_code, 404, (body, response.data))
        # own task, wrong body → 400; the task stays DELIVERED
        response = self.act(self.owner_a, task_id, "confirm", {})
        self.assertEqual(response.data, {"received_ok": ["Qabul qilinganini belgilang."]})
        self.assertEqual(LogisticsTask.objects.get(pk=task_id).status, TaskStatus.DELIVERED)

    def test_status_conflict_wins_over_body_errors(self):
        task_id = self.create_task(10)["id"]
        response = self.act(self.owner_a, task_id, "confirm", {"received_ok": False})
        self.assertEqual(response.status_code, 409, response.data)
        self.assertEqual(response.data["detail"], "Topshiriq holati: Yangi. Bu amalni bajarib bo‘lmaydi.")

    def test_owner_list_hides_staff_fields(self):
        self.create_task(10, vehicle=self.vehicle.pk, driver=self.driver.pk)
        self.login_as(self.owner_a)
        row = self.client.get("/api/logistics/tasks").data["results"][0]
        self.assertNotIn("responsible", row)
        self.assertNotIn("created_by", row)
        self.assertEqual(row["allowed_actions"], [])
        self.assertEqual(row["driver"]["full_name"], "Sardor Rahimov")  # the owner may call the driver

    def test_owner_cannot_reach_staff_actions_or_fleet(self):
        task_id = self.create_task(10)["id"]
        for action in ("assign", "accept", "start", "deliver", "cancel"):
            self.assertEqual(self.act(self.owner_a, task_id, action, {}).status_code, 403, action)
        self.login_as(self.owner_a)
        self.assertEqual(self.client.patch(f"/api/logistics/tasks/{task_id}", {"note": "x"}, format="json").status_code,
                         403)
        self.assertEqual(self.client.get("/api/logistics/vehicles").status_code, 403)
        self.assertEqual(self.client.get("/api/logistics/summary").status_code, 403)


class RejectedActionsHaveNoSideEffectsTests(LogisticsTestCase):
    def snapshot(self, task_id: int) -> tuple:
        task = LogisticsTask.objects.get(pk=task_id)
        feed = Product.objects.get(pk=self.feed.pk)
        return (
            task.status,
            task.updated_at,
            LogisticsTaskStatusLog.objects.filter(task_id=task_id).count(),
            AuditLog.objects.count(),
            StockTransaction.objects.count(),
            feed.balance,
            feed.reserved,
        )

    def test_400_403_409_change_nothing_and_notify_nobody(self):
        task_id = self.create_task(10)["id"]
        self.to_status(task_id, "IN_TRANSIT")
        before = self.snapshot(task_id)
        notifications = Notification.objects.count()
        with self.captureOnCommitCallbacks(execute=True):
            self.assertEqual(self.act(self.logist, task_id, "deliver", {}).status_code, 400)  # proof required
            self.assertEqual(self.act(self.logist2, task_id, "deliver", {}).status_code, 403)  # other worker
            self.assertEqual(self.act(self.logist, task_id, "start", {}).status_code, 409)  # replay
            self.assertEqual(self.act(self.admin, task_id, "cancel", {"reason": "ab"}).status_code, 400)
            # somebody else's upload cannot be bound as proof
            foreign = self.upload_proof(self.admin)
            response = self.act(self.logist, task_id, "deliver", {"file_ids": [foreign]})
            self.assertEqual(response.status_code, 400, response.data)
            self.assertIn("file_ids", response.data)
        after = self.snapshot(task_id)
        # the only new audit row is the admin's file upload
        self.assertEqual(after[3], before[3] + 1)
        self.assertEqual((after[:3], after[4:]), (before[:3], before[4:]))
        self.assertEqual(Notification.objects.count(), notifications)


class ReassignTests(LogisticsTestCase):
    def test_reassign_after_accept_needs_new_accept_and_notifies_new_driver(self):
        task_id = self.create_task(10, vehicle=self.vehicle.pk, driver=self.driver.pk)["id"]
        self.assertEqual(self.act(self.logist, task_id, "accept").status_code, 200)
        self.driver2.user = self.logist2
        self.driver2.save()
        with self.captureOnCommitCallbacks(execute=True):
            response = self.act(self.admin, task_id, "assign", {"vehicle": self.vehicle.pk, "driver": self.driver2.pk})
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["status"], "ASSIGNED")
        self.assertIsNone(response.data["accepted_at"])
        self.assertEqual(response.data["responsible"]["id"], self.logist2.pk)
        task = LogisticsTask.objects.get(pk=task_id)
        self.assertEqual(task.assign_seq, 2)
        row = Notification.objects.get(event="driver_assigned", recipient=self.logist2)
        self.assertTrue(row.event_key.startswith(f"driver_assigned:{task_id}.2:"))
        # the previous worker lost the task; the new one accepts and starts
        self.assertEqual(self.act(self.logist, task_id, "accept").status_code, 403)
        self.assertEqual(self.act(self.logist2, task_id, "accept").status_code, 200)
        self.assertEqual(self.act(self.logist2, task_id, "start").status_code, 200)
        self.assertEqual(StockTransaction.objects.filter(reference_id=task_id, type="OUT").count(), 1)

    def test_logistics_assigning_unlinked_driver_becomes_responsible(self):
        task_id = self.create_task(10)["id"]
        response = self.act(self.logist2, task_id, "assign", {"vehicle": self.vehicle.pk, "driver": self.driver2.pk})
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["responsible"]["id"], self.logist2.pk)
        audit = AuditLog.objects.filter(module=AuditModule.LOGISTICS, object_id=task_id).order_by("-id").first()
        self.assertEqual(audit.before, {"status": "NEW"})
        self.assertEqual(audit.after["status"], "ASSIGNED")
        self.assertEqual(audit.after["driver_id"], self.driver2.pk)


class NotificationTextTests(LogisticsTestCase):
    def test_admin_confirm_reason_is_not_called_a_problem(self):
        task_id = self.create_task(10)["id"]
        self.to_status(task_id, "DELIVERED")
        with self.captureOnCommitCallbacks(execute=True):
            response = self.act(self.admin, task_id, "confirm",
                                {"received_ok": True, "issue_note": "Egasi telefonda tasdiqladi"})
        self.assertEqual(response.status_code, 200, response.data)
        text = Notification.objects.filter(event="delivery_confirmed").first().text
        self.assertIn("Qabul izohi: Egasi telefonda tasdiqladi", text)
        self.assertNotIn("Muammo", text)
        self.assertIn(f"LG-{task_id:06d}", text)

    def test_task_texts_have_contract_rows(self):
        with self.captureOnCommitCallbacks(execute=True):
            task_id = self.create_task(150, vehicle=self.vehicle.pk, driver=self.driver.pk,
                                       planned_date=str(timezone.localdate() + timedelta(days=2)))["id"]
        text = Notification.objects.filter(event="driver_assigned").first().text
        planned = (timezone.localdate() + timedelta(days=2)).strftime("%d.%m.%Y")
        for line in (f"Topshiriq: LG-{task_id:06d}", "Mahsulotlar: Yem PK-1 — 150 kg",
                     "Transport: 01 A 123 BC, Isuzu NPR", "Haydovchi: Sardor Rahimov, +998901112233",
                     f"Reja sanasi: {planned}", f"#LG-{task_id:06d}"):
            self.assertIn(line, text)


@override_settings(BOT_API_TOKEN=BOT_API_TOKEN)
class BotIsolationTests(LogisticsTestCase):
    def bot(self, url: str, data: dict, **kwargs):
        return self.client.post(url, data, HTTP_X_BOT_TOKEN=BOT_API_TOKEN, **kwargs)

    def test_bot_owner_scope_and_upload_purpose(self):
        task_id = self.create_task(10)["id"]
        self.to_status(task_id, "DELIVERED")
        self.owner_b.telegram_id = 9002
        self.owner_b.save()
        self.client.force_authenticate(None)
        response = self.bot(f"/api/bot/tasks/{task_id}/confirm", {"telegram_id": 9002, "received_ok": True},
                            format="json")
        self.assertEqual(response.status_code, 404, response.data)
        # an owner cannot upload delivery proof through the bot either
        response = self.bot("/api/bot/files", {"telegram_id": 9002, "purpose": "DELIVERY_PROOF",
                                               "file": SimpleUploadedFile("foto.png", PNG)}, format="multipart")
        self.assertEqual(response.status_code, 403, response.data)
        self.assertEqual(LogisticsTask.objects.get(pk=task_id).status, TaskStatus.DELIVERED)


class BudgetAndIntegrityTests(LogisticsTestCase):
    def test_detail_query_budget_is_constant(self):
        def count_queries(task_id: int) -> int:
            self.login_as(self.admin)
            with CaptureQueriesContext(connection) as ctx:
                self.assertEqual(self.client.get(f"/api/logistics/tasks/{task_id}").status_code, 200)
            return len(ctx.captured_queries)

        small = self.create_task(5)["id"]
        self.login_as(self.admin)
        body = {"household": self.household_a.pk, "items": [{"product": self.feed.pk, "quantity": 5},
                                                             {"product": self.vitamin.pk, "quantity": 2}]}
        big = self.client.post("/api/logistics/tasks", body, format="json").data["id"]
        self.to_status(big, "DELIVERED")
        self.assertLessEqual(count_queries(small), 10)
        self.assertEqual(count_queries(small), count_queries(big))

    def test_integrity_clean_after_every_flow(self):
        confirmed = self.create_task(10)["id"]
        self.to_status(confirmed, "CONFIRMED")
        returned = self.create_task(20)["id"]
        self.to_status(returned, "IN_TRANSIT")
        self.assertEqual(self.act(self.admin, returned, "cancel", {"reason": "Yo‘l yopiq"}).status_code, 200)
        released = self.create_task(30)["id"]
        self.to_status(released, "ACCEPTED")
        self.assertEqual(self.act(self.admin, released, "cancel", {"reason": "Kerak emas"}).status_code, 200)
        self.create_task(40)  # still reserved
        self.assertEqual(integrity.problems(), [])
        self.assertEqual(Product.objects.get(pk=self.feed.pk).reserved, 40)

    def test_dashboard_counts_one_query(self):
        today = timezone.localdate()
        late = self.create_task(10, planned_date=str(today))["id"]
        LogisticsTask.objects.filter(pk=late).update(planned_date=today - timedelta(days=3))
        delivered = self.create_task(10)["id"]
        self.to_status(delivered, "DELIVERED")
        self.create_task(10, vehicle=self.vehicle.pk, driver=self.driver.pk)
        with self.assertNumQueries(1):
            counts = selectors.dashboard_counts()
        self.assertEqual(counts, {"new": 1, "assigned": 1, "accepted": 0, "in_transit": 0, "delivered": 1,
                                  "overdue": 1, "delivered_today": 1})

    def test_summary_on_time_and_lead_time(self):
        today = timezone.localdate()
        on_time = self.create_task(10, planned_date=str(today))["id"]
        self.to_status(on_time, "DELIVERED")
        late = self.create_task(10, planned_date=str(today))["id"]
        self.to_status(late, "DELIVERED")
        LogisticsTask.objects.filter(pk=late).update(planned_date=today - timedelta(days=1))
        no_plan = self.create_task(10)["id"]
        self.to_status(no_plan, "DELIVERED")
        LogisticsTask.objects.filter(pk=no_plan).update(created_at=timezone.now() - timedelta(hours=30))
        data = selectors.summary(self.admin)["last_30d"]
        self.assertEqual(data["on_time_pct"], 50.0)  # tasks without a plan date are not counted
        self.assertEqual(data["delivered"], 3)
        self.assertEqual(data["avg_lead_time_h"], 10.0)  # (0 + 0 + 30) / 3

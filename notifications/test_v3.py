"""Notification engine V3 (API_CONTRACT_V3 §9, §14.1, §16.1): inbox rows, settings, monitor, bell, reminders."""

from __future__ import annotations

from datetime import date, datetime, timedelta

from django.db import connection
from django.test import override_settings
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from accounts.models import User
from audit.models import AuditLog
from common.roles import Role
from common.testing import BOT_API_TOKEN, BaseAPITestCase, make_user
from core.models import SystemSettings

from . import registry, reminders, services
from .models import Notification, NotificationSetting, NotificationStatus


def queue(event: str = "batch_created", key: int = 1, **kwargs) -> list[Notification]:
    kwargs.setdefault("text", "🐣 <b>NNP — YANGI PARRANDA PARTIYASI</b>\n\nPartiya: P-1 &amp; Co")
    kwargs.setdefault("staff_path", "/batches/1")
    return services.notify(event, key=key, **kwargs)


class EngineV3Tests(BaseAPITestCase):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        User.objects.filter(pk=cls.admin.pk).update(telegram_id=7001)
        cls.admin.refresh_from_db()
        cls.vet = make_user("vet1", Role.VET, telegram_id=7002)
        cls.logistics = make_user("logist1", Role.LOGISTICS)

    def test_rows_for_linked_and_unlinked_recipients(self):
        queue()
        rows = {row.recipient_id: row for row in Notification.objects.filter(event="batch_created")}
        self.assertEqual(set(rows), {self.admin.pk, self.warehouse.pk})
        self.assertEqual((rows[self.admin.pk].status, rows[self.admin.pk].chat_id), ("PENDING", 7001))
        self.assertEqual((rows[self.warehouse.pk].status, rows[self.warehouse.pk].chat_id), ("SKIPPED", None))
        row = rows[self.admin.pk]
        self.assertEqual(row.title, "NNP — YANGI PARRANDA PARTIYASI")
        self.assertEqual(row.body, "Partiya: P-1 & Co")
        queue()  # idempotent
        self.assertEqual(Notification.objects.count(), 2)

    def test_disabled_event_queues_nothing(self):
        NotificationSetting.objects.create(event="batch_created", enabled=False)
        self.assertEqual(queue(), [])
        self.assertFalse(Notification.objects.exists())

    def test_telegram_disabled_gives_skipped_rows(self):
        NotificationSetting.objects.create(event="batch_created", telegram_enabled=False)
        queue()
        self.assertEqual(set(Notification.objects.values_list("status", flat=True)), {"SKIPPED"})

    def test_roles_override_and_contextual_switch(self):
        NotificationSetting.objects.create(event="poultry_death", roles=["VET"], contextual_enabled=False)
        services.notify("poultry_death", key=9, text="x\ny", staff_path="/households/1",
                        contextual={registry.HOUSEHOLD_OWNER: self.owner_a})
        self.assertEqual(list(Notification.objects.values_list("recipient_id", flat=True)), [self.vet.pk])

    def test_poultry_death_default_includes_vets(self):
        services.notify("poultry_death", key=10, text="x\ny", staff_path="/households/1",
                        contextual={registry.HOUSEHOLD_OWNER: self.owner_a})
        recipients = set(Notification.objects.values_list("recipient_id", flat=True))
        self.assertEqual(recipients, {self.admin.pk, self.vet.pk, self.owner_a.pk})
        self.assertEqual(Notification.objects.get(recipient=self.owner_a).button_path, "/my")

    def test_low_stock_master_switch(self):
        SystemSettings.objects.filter(pk=1).update(low_stock_alert_enabled=False)
        self.assertEqual(queue("low_stock"), [])
        self.assertEqual(queue("stock_running_low"), [])
        SystemSettings.objects.filter(pk=1).update(low_stock_alert_enabled=True)
        self.assertEqual(len(queue("low_stock")), 2)

    def test_fallback_roles_without_contextual_users(self):
        services.notify("driver_assigned", key="5.1", text="x\ny", staff_path="/logistics/5")
        self.assertEqual(list(Notification.objects.values_list("recipient_id", flat=True)), [self.logistics.pk])

    def test_unknown_contextual_name_fails_loudly(self):
        with self.assertRaises(ValueError):
            services.notify_on_commit("batch_created", key=1, text="x", contextual={registry.HOUSEHOLD_OWNER: None})

    def test_bot_queue_returns_only_pending(self):
        queue()
        with override_settings(BOT_API_TOKEN=BOT_API_TOKEN):
            response = self.client.get("/api/bot/notifications/pending", HTTP_X_BOT_TOKEN=BOT_API_TOKEN)
        self.assertEqual([row["chat_id"] for row in response.data], [7001])


class SettingsApiTests(BaseAPITestCase):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.root = make_user("root1", Role.ADMIN, is_superuser=True, is_staff=True)

    def test_list_in_registry_order(self):
        self.login_as(self.admin)
        response = self.client.get("/api/notification-settings")
        self.assertEqual(response.status_code, 200)
        self.assertEqual([row["event"] for row in response.data], list(registry.EVENTS))
        order = next(row for row in response.data if row["event"] == "order_created")
        self.assertEqual(
            order,
            {"event": "order_created", "display": "Yangi buyurtma berildi",
             "description": "Xonadon egasi buyurtma yoki talab yuborganda", "enabled": True,
             "telegram_enabled": True, "default_roles": ["ADMIN"], "roles": ["ADMIN"], "roles_overridden": False,
             "contextual": [], "contextual_enabled": True, "updated_by": None, "updated_at": None,
             # V4 §4 notification constructor
             "custom_text": "", "delay_minutes": 0, "extra_user_ids": []},
        )
        self.login_as(self.warehouse)
        self.assertEqual(self.client.get("/api/notification-settings").status_code, 403)

    def test_patch_super_admin_only_with_audit(self):
        self.login_as(self.admin)
        response = self.client.patch("/api/notification-settings/order_created", {"enabled": False}, format="json")
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.data["detail"], "Bu amal faqat Super Admin uchun.")
        self.login_as(self.root)
        response = self.client.patch(
            "/api/notification-settings/order_created", {"roles": ["WAREHOUSE", "ADMIN"], "telegram_enabled": False},
            format="json",
        )
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["roles"], ["ADMIN", "WAREHOUSE"])
        self.assertTrue(response.data["roles_overridden"])
        self.assertEqual(response.data["updated_by"]["id"], self.root.pk)
        log = AuditLog.objects.get(module="notifications", action="update")
        self.assertEqual(log.object_type, "NotificationSetting")
        response = self.client.patch("/api/notification-settings/order_created", {"roles": None}, format="json")
        self.assertFalse(response.data["roles_overridden"])
        self.assertEqual(response.data["roles"], ["ADMIN"])

    def test_patch_errors(self):
        self.login_as(self.root)
        self.assertEqual(
            self.client.patch("/api/notification-settings/nope", {"enabled": False}, format="json").status_code, 404
        )
        response = self.client.patch("/api/notification-settings/order_created", {"roles": ["OWNER"]}, format="json")
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.data, {"roles": ["“OWNER” ruxsat etilgan qiymat emas."]})


class MonitorTests(BaseAPITestCase):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        User.objects.filter(pk=cls.owner_a.pk).update(telegram_id=8001)
        cls.owner_a.refresh_from_db()

    def failed_row(self, user: User, key: str = "1", chat_id: int | None = 1) -> Notification:
        return Notification.objects.create(
            recipient=user, chat_id=chat_id, event="order_approved", event_key=f"order_approved:{key}:{user.pk}",
            text="<b>t</b>\nb", title="NNP — BUYURTMA TASDIQLANDI", related_op="BY-000005",
            status=NotificationStatus.FAILED, attempts=5, last_error="BotBlocked",
        )

    def test_list_filters_and_shape(self):
        row = self.failed_row(self.owner_a)
        queue()
        self.login_as(self.admin)
        response = self.client.get("/api/notifications?status=FAILED")
        self.assertEqual(response.data["count"], 1)
        item = response.data["results"][0]
        self.assertEqual(item["id"], row.pk)
        self.assertEqual(item["recipient"], {"id": self.owner_a.pk, "full_name": self.owner_a.full_name,
                                             "role": "OWNER"})
        self.assertEqual((item["event_display"], item["status_display"]), ("Buyurtma tasdiqlandi", "Yuborilmadi"))
        self.assertTrue(item["telegram_linked"])
        self.assertEqual(self.client.get("/api/notifications?search=BY-0000").data["count"], 1)
        self.assertEqual(self.client.get(f"/api/notifications?recipient={self.warehouse.pk}").data["count"], 1)
        self.assertEqual(self.client.get("/api/notifications?status=BAD").status_code, 400)
        detail = self.client.get(f"/api/notifications/{row.pk}")
        self.assertEqual(detail.data["text"], "<b>t</b>\nb")
        stats = self.client.get("/api/notifications/stats").data
        self.assertEqual((stats["FAILED"], stats["SKIPPED"]), (1, 2))
        self.assertIn("by_event", stats)
        self.login_as(self.warehouse)
        self.assertEqual(self.client.get("/api/notifications").status_code, 403)

    def test_list_query_budget(self):
        for n in range(5):
            self.failed_row(self.owner_a, key=str(n))
        self.login_as(self.admin)
        with CaptureQueriesContext(connection) as ctx:
            self.client.get("/api/notifications")
        self.assertLessEqual(len(ctx.captured_queries), 6)

    def test_retry_rules(self):
        row = self.failed_row(self.owner_a)
        self.login_as(self.admin)
        response = self.client.post(f"/api/notifications/{row.pk}/retry")
        self.assertEqual(response.status_code, 200)
        row.refresh_from_db()
        self.assertEqual((row.status, row.attempts, row.chat_id, row.last_error), ("PENDING", 0, 8001, "BotBlocked"))
        again = self.client.post(f"/api/notifications/{row.pk}/retry")
        self.assertEqual(again.status_code, 409)
        self.assertEqual(again.data["detail"], "Faqat yuborilmagan (FAILED) xabarni qayta yuborish mumkin.")
        unlinked = self.failed_row(self.owner_b)
        response = self.client.post(f"/api/notifications/{unlinked.pk}/retry")
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.data["detail"], "Foydalanuvchi Telegramga bog‘lanmagan.")
        self.assertEqual(self.client.post("/api/notifications/999999/retry").status_code, 404)
        self.assertEqual(AuditLog.objects.filter(module="notifications", action="retry").count(), 1)
        self.assertEqual(AuditLog.objects.get(action="retry").op_id, "BY-000005")

    def test_retry_failed_bulk(self):
        self.failed_row(self.owner_a, key="a")
        self.failed_row(self.owner_a, key="b")
        self.failed_row(self.owner_b, key="c")
        self.login_as(self.admin)
        response = self.client.post("/api/notifications/retry-failed", {"event": "order_approved"}, format="json")
        self.assertEqual(response.data, {"retried": 2, "skipped": 1})


class MyNotificationsTests(BaseAPITestCase):
    def test_bell_flow_and_isolation(self):
        queue(key=1)
        queue(key=2)
        own = Notification.objects.filter(recipient=self.warehouse).order_by("id")
        other = Notification.objects.get(recipient=self.admin, event_key__startswith="batch_created:1:")
        self.login_as(self.warehouse)
        with CaptureQueriesContext(connection) as ctx:
            count = self.client.get("/api/my/notifications/unread-count")
        self.assertEqual(count.data, {"count": 2})
        self.assertEqual(len(ctx.captured_queries), 1)
        listing = self.client.get("/api/my/notifications")
        self.assertEqual([row["id"] for row in listing.data["results"]], [own[1].pk, own[0].pk])
        self.assertEqual(listing.data["results"][0]["event_display"], "Yangi parranda partiyasi")
        self.assertEqual(self.client.post(f"/api/my/notifications/{other.pk}/read").status_code, 404)
        read = self.client.post(f"/api/my/notifications/{own[0].pk}/read")
        self.assertIsNotNone(read.data["read_at"])
        self.assertEqual(self.client.get("/api/my/notifications?unread=1").data["count"], 1)
        self.assertEqual(self.client.post("/api/my/notifications/read-all").data, {"updated": 1})
        self.assertEqual(self.client.get("/api/my/notifications/unread-count").data, {"count": 0})
        other.refresh_from_db()
        self.assertIsNone(other.read_at)
        self.assertEqual(own[0].status, "SKIPPED")  # reading never changes delivery status

    def test_every_role_has_a_bell(self):
        for role in (Role.LOGISTICS, Role.VET):
            self.login_as(make_user(f"bell_{role.lower()}", role))
            self.assertEqual(self.client.get("/api/my/notifications").status_code, 200)


@override_settings(BOT_API_TOKEN=BOT_API_TOKEN)
class BotV3Tests(BaseAPITestCase):
    def bot(self, method: str, path: str, data=None):
        return getattr(self.client, method)(path, data, format="json", HTTP_X_BOT_TOKEN=BOT_API_TOKEN)

    def test_bot_inbox(self):
        User.objects.filter(pk=self.warehouse.pk).update(telegram_id=9100)
        for key in range(12):
            queue(key=key)
        response = self.bot("get", "/api/bot/users/9100/notifications?limit=50")
        self.assertEqual(len(response.data), 12)  # limit clamped to 20
        self.assertEqual(set(response.data[0]), {"id", "title", "body", "button_path", "created_at", "read_at"})
        self.assertEqual(len(self.bot("get", "/api/bot/users/9100/notifications?limit=3").data), 3)
        self.assertEqual(self.bot("get", "/api/bot/users/424242/notifications").status_code, 404)
        User.objects.filter(pk=self.warehouse.pk).update(is_active=False)
        self.assertEqual(self.bot("get", "/api/bot/users/9100/notifications").status_code, 403)

    def test_reminder_tick_endpoint(self):
        response = self.bot("post", "/api/bot/reminders/run", {})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(set(response.data), {"date", "ran", "queued"})


class ReminderGuardTests(BaseAPITestCase):
    def setUp(self):
        super().setUp()
        self.calls: list[date] = []
        self._saved = dict(reminders._JOBS)
        reminders._JOBS.clear()
        reminders.register_job("test", lambda today: self.calls.append(today) or 3)

    def tearDown(self):
        reminders._JOBS.clear()
        reminders._JOBS.update(self._saved)
        super().tearDown()

    def at(self, day: date, hour: int) -> datetime:
        return timezone.make_aware(datetime(day.year, day.month, day.day, hour, 0))

    def test_runs_once_per_day_after_reminder_hour(self):
        day = date(2026, 11, 4)
        self.assertEqual(reminders.run_if_due(self.at(day, 7)), {"date": "2026-11-04", "ran": False, "queued": 0})
        self.assertEqual(reminders.run_if_due(self.at(day, 8)), {"date": "2026-11-04", "ran": True, "queued": 3})
        self.assertFalse(reminders.run_if_due(self.at(day, 9))["ran"])
        self.assertEqual(SystemSettings.objects.get().last_reminders_date, day)
        self.assertTrue(reminders.run_if_due(self.at(day, 9), force=True)["ran"])
        self.assertTrue(reminders.run_if_due(self.at(day + timedelta(days=1), 8))["ran"])
        self.assertEqual(self.calls, [day, day, day + timedelta(days=1)])

    def test_guard_only_moves_forward(self):
        day = date(2026, 11, 4)
        self.assertTrue(reminders.run_if_due(self.at(day, 9))["ran"])
        # A forced run for an earlier day does not move the guard back ...
        self.assertTrue(reminders.run_if_due(self.at(day - timedelta(days=3), 23), force=True)["ran"])
        self.assertEqual(SystemSettings.objects.get().last_reminders_date, day)
        # ... and a manual run that must not touch it leaves it alone.
        self.assertTrue(reminders.run_if_due(self.at(day + timedelta(days=1), 9), force=True, move_guard=False)["ran"])
        self.assertEqual(SystemSettings.objects.get().last_reminders_date, day)
        self.assertTrue(reminders.run_if_due(self.at(day + timedelta(days=1), 9))["ran"])

    def test_command_rejects_future_date_and_past_date_keeps_guard(self):
        from io import StringIO

        from django.core.management import call_command
        from django.core.management.base import CommandError

        tomorrow = timezone.localdate() + timedelta(days=1)
        with self.assertRaises(CommandError):
            call_command("send_reminders", "--date", tomorrow.isoformat(), stdout=StringIO())
        self.assertEqual(self.calls, [])
        self.assertIsNone(SystemSettings.objects.get().last_reminders_date)
        past = timezone.localdate() - timedelta(days=2)
        call_command("send_reminders", "--date", past.isoformat(), stdout=StringIO())
        self.assertEqual(self.calls, [past])
        # The real daily run (guard) is still due today.
        self.assertIsNone(SystemSettings.objects.get().last_reminders_date)

    def test_failing_job_does_not_stop_others(self):
        def boom(today):
            raise RuntimeError("x")

        reminders.register_job("boom", boom, order=1)
        with self.assertLogs("nnp.reminders", level="ERROR"):
            result = reminders.run_if_due(self.at(date(2026, 11, 5), 10))
        self.assertEqual(result["queued"], 3)

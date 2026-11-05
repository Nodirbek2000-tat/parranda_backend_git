from __future__ import annotations

from django.test import override_settings
from django.utils import timezone

from accounts.models import User
from common.roles import Role
from common.testing import BOT_API_TOKEN, BaseAPITestCase, make_user

from . import services
from .models import Notification, NotificationStatus


class NotificationEngineTests(BaseAPITestCase):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        User.objects.filter(pk=cls.admin.pk).update(telegram_id=1001)
        User.objects.filter(pk=cls.warehouse.pk).update(telegram_id=1002)
        User.objects.filter(pk=cls.owner_a.pk).update(telegram_id=1003)
        # No Telegram link → in-app only row (SKIPPED, V3 §9.1); blocked → no row at all.
        cls.admin_no_tg = make_user("admin_no_tg", Role.ADMIN)
        make_user("admin_blocked", Role.ADMIN, telegram_id=1004, is_active=False)

    def distribute(self, quantity: int = 50):
        return self.client.post(
            "/api/movements",
            {"batch": self.batch.pk, "household": self.household_a.pk, "type": "DISTRIBUTE", "quantity": quantity},
            format="json",
        )

    def test_queued_only_after_commit_and_idempotent(self):
        self.login_as(self.admin)
        with self.captureOnCommitCallbacks(execute=False) as callbacks:
            response = self.distribute()
        self.assertEqual(response.status_code, 201)
        self.assertEqual(Notification.objects.count(), 0)  # nothing before commit

        for callback in callbacks:
            callback()
        all_rows = Notification.objects.filter(event="poultry_distributed")
        rows = all_rows.filter(chat_id__isnull=False)  # Telegram recipients (V3 §18.4)
        self.assertEqual(
            {(n.recipient_id, n.button_path) for n in rows},
            {
                (self.admin.pk, f"/households/{self.household_a.pk}"),
                (self.warehouse.pk, f"/households/{self.household_a.pk}"),
                (self.owner_a.pk, "/my"),
            },
        )
        skipped = all_rows.get(recipient=self.admin_no_tg)
        self.assertEqual((skipped.status, skipped.chat_id), (NotificationStatus.SKIPPED, None))
        self.assertFalse(all_rows.filter(recipient__username="admin_blocked").exists())
        for callback in callbacks:  # replay → no duplicates
            callback()
        self.assertEqual(rows.count(), 3)
        self.assertEqual(all_rows.count(), 4)
        movement_id = response.data["id"]
        self.assertTrue(
            Notification.objects.filter(event_key=f"poultry_distributed:{movement_id}:{self.owner_a.pk}").exists()
        )

    def test_rollback_queues_nothing(self):
        self.login_as(self.admin)
        with self.captureOnCommitCallbacks(execute=True):
            response = self.distribute(quantity=10_000)
        self.assertEqual(response.status_code, 400)
        self.assertEqual(Notification.objects.count(), 0)

    def test_text_format(self):
        self.login_as(self.admin)
        with self.captureOnCommitCallbacks(execute=True):
            response = self.distribute(quantity=1500 - 1000)  # 500 birds
        notification = Notification.objects.get(event="poultry_distributed", recipient=self.owner_a)
        expected = (
            "🐔 <b>NNP — PARRANDA TARQATILDI</b>\n"
            "\n"
            f"Xonadon: Navbahor 12-uy ({self.household_a.code})\n"
            "Mas’ul: Bobur Aliyev\n"
            "Parranda turi: Go‘sht yo‘nalishi\n"
            "Miqdori: 500 dona\n"
            f"Partiya: {self.batch.code}\n"
            f"Sana: {timezone.localdate():%d.%m.%Y}\n"
            "\n"
            "Holat: Tarqatildi\n"
            f"Operatsiya ID: #{response.data['op_id']}"
        )
        self.assertEqual(notification.text, expected)
        self.assertEqual(notification.chat_id, 1003)
        self.assertEqual(notification.related_op, response.data["op_id"])

    def test_user_values_are_html_escaped(self):
        self.household_a.name = "<script>alert(1)</script> & Co"
        self.household_a.save()
        self.login_as(self.admin)
        with self.captureOnCommitCallbacks(execute=True):
            self.distribute()
        text = Notification.objects.filter(event="poultry_distributed").first().text
        self.assertIn("&lt;script&gt;alert(1)&lt;/script&gt; &amp; Co", text)
        self.assertNotIn("<script>", text)

    def test_household_and_report_events(self):
        self.login_as(self.admin)
        with self.captureOnCommitCallbacks(execute=True):
            response = self.client.post(
                "/api/households",
                {"name": "Yangi uy", "owner_first_name": "A", "owner_last_name": "B", "owner_username": "yangi1",
                 "phone": "+998901234567", "region": "Samarqand viloyati", "district": "Navobod tumani"},
                format="json",
            )
        hid = response.data["household"]["id"]
        created = Notification.objects.get(event="household_created", chat_id__isnull=False)
        self.assertEqual((created.recipient_id, created.button_path), (self.admin.pk, f"/households/{hid}"))

        self.login_as(self.owner_a)
        with self.captureOnCommitCallbacks(execute=True):
            self.client.post("/api/daily-reports", {"feed_kg": 10, "meat_kg": 5, "deaths": 2}, format="json")
        report = Notification.objects.get(event="daily_report", chat_id__isnull=False)
        self.assertEqual(report.recipient_id, self.admin.pk)
        self.assertIn("📝 <b>NNP — KUNLIK HISOBOT</b>", report.text)
        deaths = Notification.objects.filter(event="poultry_death", chat_id__isnull=False)
        self.assertEqual(
            {(n.recipient_id, n.button_path) for n in deaths},
            {(self.admin.pk, f"/households/{self.household_a.pk}"), (self.owner_a.pk, "/my")},
        )


@override_settings(BOT_API_TOKEN=BOT_API_TOKEN)
class BotEndpointTests(BaseAPITestCase):
    def bot(self, method: str, url: str, data=None, token: str | None = BOT_API_TOKEN):
        headers = {"HTTP_X_BOT_TOKEN": token} if token is not None else {}
        return getattr(self.client, method)(url, data, format="json", **headers)

    def test_wrong_or_missing_token_is_403(self):
        self.assertEqual(self.bot("get", "/api/bot/notifications/pending", token="wrong").status_code, 403)
        self.assertEqual(self.bot("get", "/api/bot/notifications/pending", token=None).status_code, 403)
        self.assertEqual(self.bot("get", "/api/bot/notifications/pending").status_code, 200)

    @override_settings(BOT_API_TOKEN="")
    def test_empty_configured_token_refuses_everything(self):
        self.assertEqual(self.bot("get", "/api/bot/notifications/pending", token="").status_code, 403)
        self.assertEqual(self.bot("post", "/api/bot/unlink", {"telegram_id": 1}, token="").status_code, 403)

    def test_jwt_user_cannot_use_bot_endpoints(self):
        self.login_as(self.admin)
        self.assertEqual(self.client.get("/api/bot/notifications/pending").status_code, 403)

    def test_link_flow(self):
        self.assertEqual(self.bot("get", "/api/bot/users/700").status_code, 404)
        wrong = self.bot("post", "/api/bot/link", {"telegram_id": 700, "username": "owner_a", "password": "x"})
        self.assertEqual(wrong.status_code, 400)
        self.assertEqual(wrong.data, {"detail": "Login yoki parol noto‘g‘ri"})

        linked = self.bot("post", "/api/bot/link", {"telegram_id": 700, "username": "owner_a", "password": "Parol12345"})
        self.assertEqual(linked.status_code, 200)
        self.assertTrue(linked.data["telegram_linked"])
        self.assertEqual(linked.data["household_id"], self.household_a.pk)
        self.assertEqual(self.bot("get", "/api/bot/users/700").data["username"], "owner_a")

        # The same Telegram account re-linked to another user moves the link.
        moved = self.bot("post", "/api/bot/link", {"telegram_id": 700, "username": "owner_b", "password": "Parol12345"})
        self.assertEqual(moved.status_code, 200)
        self.owner_a.refresh_from_db()
        self.assertIsNone(self.owner_a.telegram_id)

        unlinked = self.bot("post", "/api/bot/unlink", {"telegram_id": 700})
        self.assertEqual(unlinked.status_code, 200)
        self.assertEqual(self.bot("get", "/api/bot/users/700").status_code, 404)

    def test_link_blocked_user_is_403(self):
        User.objects.filter(pk=self.owner_a.pk).update(is_active=False)
        response = self.bot("post", "/api/bot/link", {"telegram_id": 700, "username": "owner_a", "password": "Parol12345"})
        self.assertEqual(response.status_code, 403)

    def test_location(self):
        User.objects.filter(pk=self.owner_a.pk).update(telegram_id=801)
        User.objects.filter(pk=self.admin.pk).update(telegram_id=802)
        response = self.bot("post", "/api/bot/location", {"telegram_id": 801, "latitude": 39.6512345, "longitude": 66.97})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.data["household"],
            {"id": self.household_a.pk, "code": self.household_a.code, "name": "Navbahor 12-uy",
             "latitude": 39.651235, "longitude": 66.97},
        )
        forbidden = self.bot("post", "/api/bot/location", {"telegram_id": 802, "latitude": 39.6, "longitude": 66.9})
        self.assertEqual(forbidden.status_code, 403)
        self.assertEqual(forbidden.data["detail"], "Lokatsiyani faqat xonadon egasi yubora oladi")
        unknown = self.bot("post", "/api/bot/location", {"telegram_id": 803, "latitude": 39.6, "longitude": 66.9})
        self.assertEqual(unknown.status_code, 404)

    def test_pending_and_result(self):
        User.objects.filter(pk=self.admin.pk).update(telegram_id=901)
        self.admin.refresh_from_db()
        rows = services.notify("batch_created", key=1, text="<b>1</b>", staff_path="/batches", related_op="NNP-1")
        first = next(row for row in rows if row.recipient_id == self.admin.pk)
        rows = services.notify("batch_created", key=2, text="<b>2</b>", staff_path="/batches", related_op="NNP-2")
        second = next(row for row in rows if row.recipient_id == self.admin.pk)
        first, second = Notification.objects.get(event_key=first.event_key), Notification.objects.get(
            event_key=second.event_key
        )

        pending = self.bot("get", "/api/bot/notifications/pending?limit=20")
        self.assertEqual(
            pending.data,
            [
                {"id": first.pk, "chat_id": 901, "text": "<b>1</b>", "button_path": "/batches"},
                {"id": second.pk, "chat_id": 901, "text": "<b>2</b>", "button_path": "/batches"},
            ],
        )

        ok = self.bot("post", f"/api/bot/notifications/{first.pk}/result", {"ok": True})
        self.assertEqual(ok.data, {"status": "SENT"})
        first.refresh_from_db()
        self.assertIsNotNone(first.sent_at)

        with self.assertLogs("nnp.notifications", level="WARNING"):  # the final failure is logged
            for attempt in range(1, 6):
                result = self.bot(
                    "post", f"/api/bot/notifications/{second.pk}/result", {"ok": False, "error": "timeout"}
                )
                self.assertEqual(result.data["status"], "FAILED" if attempt == 5 else "PENDING")
        second.refresh_from_db()
        self.assertEqual((second.status, second.attempts, second.last_error), (NotificationStatus.FAILED, 5, "timeout"))
        self.assertEqual(self.bot("get", "/api/bot/notifications/pending").data, [])
        self.assertEqual(self.bot("post", "/api/bot/notifications/99999/result", {"ok": True}).status_code, 404)

    def test_link_is_throttled_per_telegram_id(self):
        payload = {"telegram_id": 555, "username": "owner_a", "password": "x"}
        for _ in range(10):
            self.bot("post", "/api/bot/link", payload)
        self.assertEqual(self.bot("post", "/api/bot/link", payload).status_code, 429)
        other = self.bot("post", "/api/bot/link", {**payload, "telegram_id": 556, "username": "owner_b"})
        self.assertEqual(other.status_code, 400)

    def test_link_is_throttled_per_username_across_telegram_ids(self):
        """Many throwaway Telegram accounts cannot multiply the guesses against one username."""
        for telegram_id in range(1000, 1010):
            self.bot("post", "/api/bot/link", {"telegram_id": telegram_id, "username": "owner_a", "password": "x"})
        blocked = self.bot("post", "/api/bot/link", {"telegram_id": 2000, "username": "OWNER_A", "password": "x"})
        self.assertEqual(blocked.status_code, 429)
        # The shared bucket also covers the web login of the same account.
        web = self.client.post("/api/auth/login", {"username": "owner_a", "password": "x"}, format="json")
        self.assertEqual(web.status_code, 429)

"""The bot's delivery reports and ids must never be rejected in a way that wedges the queue."""

from __future__ import annotations

from django.test import override_settings

from common.testing import BOT_API_TOKEN, BaseAPITestCase

from .models import Notification, NotificationStatus


@override_settings(BOT_API_TOKEN=BOT_API_TOKEN)
class BotRobustnessTests(BaseAPITestCase):
    headers = {"HTTP_X_BOT_TOKEN": BOT_API_TOKEN}

    def setUp(self):
        super().setUp()
        self.notification = Notification.objects.create(
            recipient=self.admin, chat_id=1001, event="test", event_key="test:1:1", text="<b>x</b>"
        )
        self.url = f"/api/bot/notifications/{self.notification.pk}/result"

    def test_long_error_text_is_accepted_and_truncated(self):
        response = self.client.post(self.url, {"ok": False, "error": "e" * 5000}, format="json", **self.headers)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data, {"status": "PENDING"})
        self.notification.refresh_from_db()
        self.assertEqual(self.notification.attempts, 1)
        self.assertEqual(len(self.notification.last_error), 2000)

    def test_null_error_is_accepted(self):
        response = self.client.post(self.url, {"ok": True, "error": None}, format="json", **self.headers)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data, {"status": NotificationStatus.SENT})

    def test_result_after_final_state_is_idempotent(self):
        self.client.post(self.url, {"ok": True}, format="json", **self.headers)
        response = self.client.post(self.url, {"ok": False, "error": "late"}, format="json", **self.headers)
        self.assertEqual(response.data, {"status": "SENT"})
        self.notification.refresh_from_db()
        self.assertEqual(self.notification.attempts, 0)

    def test_huge_ids(self):
        huge = "9" * 25
        self.assertEqual(self.client.get(f"/api/bot/users/{huge}", **self.headers).status_code, 404)
        self.assertEqual(
            self.client.post(f"/api/bot/notifications/{huge}/result", {"ok": True}, format="json", **self.headers).status_code,
            404,
        )
        response = self.client.post(
            "/api/bot/link", {"telegram_id": int(huge), "username": "a", "password": "b"}, format="json", **self.headers
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn("telegram_id", response.data)

    def test_unknown_notification_is_404(self):
        response = self.client.post("/api/bot/notifications/987654/result", {"ok": True}, format="json", **self.headers)
        self.assertEqual(response.status_code, 404)

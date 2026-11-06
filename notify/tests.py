from django.test import override_settings
from rest_framework.test import APIClient

from accounts.models import Role, User
from config.testutils import NNPTestCase

from .models import Notification


def bot(token="bot-secret") -> APIClient:
    client = APIClient()
    if token:
        client.credentials(HTTP_X_BOT_TOKEN=token)
    return client


class BotTokenTests(NNPTestCase):
    def test_token_required(self):
        for client in (bot(None), bot("wrong")):
            self.assertEqual(client.get("/api/bot/notifications/pending").status_code, 403)
            self.assertEqual(client.post("/api/bot/link", {}, format="json").status_code, 403)
        self.assertEqual(bot().get("/api/bot/notifications/pending").status_code, 200)

    @override_settings(BOT_API_TOKEN="")
    def test_empty_setting_disables_bot_api(self):
        self.assertEqual(bot("").get("/api/bot/notifications/pending").status_code, 403)
        self.assertEqual(bot("anything").get("/api/bot/notifications/pending").status_code, 403)


class BotFlowTests(NNPTestCase):
    def test_link_me_tasks_unlink(self):
        r = bot().post("/api/bot/link", {"telegram_id": 42, "username": "hr", "password": "bad"}, format="json")
        self.assertEqual(r.status_code, 401)
        self.assertIn("detail", r.json())
        self.assertEqual(bot().get("/api/bot/me/42").status_code, 404)
        r = bot().post("/api/bot/link", {"telegram_id": 42, "username": "hr", "password": "Parol-12345"},
                       format="json")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["role"], "HR")
        me = bot().get("/api/bot/me/42").json()
        for key in ("full_name", "username", "role", "role_display", "phone", "must_change_password"):
            self.assertIn(key, me)
        # relinking the same Telegram account to another user moves it
        bot().post("/api/bot/link", {"telegram_id": 42, "username": "vet", "password": "Parol-12345"}, format="json")
        self.assertIsNone(User.objects.get(username="hr").telegram_id)
        self.assertEqual(bot().get("/api/bot/me/42").json()["username"], "vet")
        bot().post("/api/bot/link", {"telegram_id": 42, "username": "hr", "password": "Parol-12345"}, format="json")

        case_id = self.create_case()["id"]
        tasks = bot().get("/api/bot/tasks/42").json()
        self.assertEqual(len(tasks), 1)
        task = tasks[0]
        self.assertEqual((task["id"], task["stage_number"], task["stage_status"], task["action"]),
                         (case_id, 2, "ACTIVE", "submit"))
        for key in ("code", "name", "stage_title", "due_at", "is_overdue"):
            self.assertIn(key, task)
        self.assertEqual(bot().get("/api/bot/tasks/999").status_code, 404)

        self.assertEqual(bot().post("/api/bot/unlink", {"telegram_id": 42}, format="json").status_code, 200)
        self.assertEqual(bot().get("/api/bot/me/42").status_code, 404)
        self.assertEqual(bot().post("/api/bot/unlink", {"telegram_id": 42}, format="json").status_code, 404)

    def test_pending_and_results(self):
        User.objects.filter(username="hr").update(telegram_id=42)
        User.objects.create_user(username="hr2", password="x", role=Role.HR)  # not linked: skipped
        self.create_case()
        self.assertEqual(Notification.objects.count(), 2)
        rows = bot().get("/api/bot/notifications/pending?limit=50").json()
        self.assertEqual(len(rows), 1)
        self.assertEqual(set(rows[0]), {"id", "chat_id", "text", "button_path"})
        self.assertEqual(rows[0]["chat_id"], 42)
        nid = rows[0]["id"]
        for attempt in range(1, 5):
            r = bot().post(f"/api/bot/notifications/{nid}/result", {"ok": False, "error": "timeout"}, format="json")
            self.assertEqual(r.json()["status"], "PENDING")
        r = bot().post(f"/api/bot/notifications/{nid}/result", {"ok": False, "error": "timeout"}, format="json")
        self.assertEqual(r.json()["status"], "FAILED")
        self.assertEqual(bot().get("/api/bot/notifications/pending").json(), [])
        self.assertEqual(bot().post("/api/bot/notifications/99999/result", {"ok": True}, format="json").status_code,
                         404)

    def test_sent_and_web_inbox(self):
        User.objects.filter(username="hr").update(telegram_id=42)
        self.create_case()
        nid = bot().get("/api/bot/notifications/pending").json()[0]["id"]
        r = bot().post(f"/api/bot/notifications/{nid}/result", {"ok": True}, format="json")
        self.assertEqual(r.json()["status"], "SENT")
        inbox = self.client_for(Role.HR).get("/api/notifications").json()
        self.assertEqual([n["id"] for n in inbox], [nid])
        self.assertIsNone(inbox[0]["read_at"])
        self.assertIsNotNone(self.client_for(Role.HR).post(f"/api/notifications/{nid}/read").json()["read_at"])
        self.assertEqual(self.client_for(Role.VET).post(f"/api/notifications/{nid}/read").status_code, 404)


class HealthTests(NNPTestCase):
    def test_health_is_public(self):
        r = APIClient().get("/api/health", HTTP_AUTHORIZATION="Bearer garbage")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["db"], "ok")

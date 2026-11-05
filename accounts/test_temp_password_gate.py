"""Server-side enforcement of the first-login password change (TZ §3 line 111)."""

from __future__ import annotations

from django.test import override_settings
from rest_framework.test import APIClient

from common.roles import Role
from common.testing import BOT_API_TOKEN, BaseAPITestCase, make_user

from .authentication import MSG_MUST_CHANGE_PASSWORD, temp_password_allows


@override_settings(BOT_API_TOKEN=BOT_API_TOKEN)
class TempPasswordGateTests(BaseAPITestCase):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.fresh = make_user("yangi_logist", Role.LOGISTICS, password="Vaqtin4ik1", must_change_password=True,
                              telegram_id=700700)

    def _jwt_client(self, password: str = "Vaqtin4ik1") -> APIClient:
        client = APIClient()
        response = client.post("/api/auth/login", {"username": "yangi_logist", "password": password}, format="json")
        self.assertEqual(response.status_code, 200, response.data)
        self.assertTrue(response.data["user"]["must_change_password"])
        client.credentials(HTTP_AUTHORIZATION=f"Bearer {response.data['access']}")
        return client

    def test_only_password_endpoints_are_reachable(self):
        client = self._jwt_client()
        self.assertEqual(client.get("/api/auth/me").status_code, 200)
        self.assertEqual(client.get("/api/settings").status_code, 200)
        for path in ("/api/logistics/tasks", "/api/my/notifications", "/api/search?q=ab"):
            response = client.get(path)
            self.assertEqual(response.status_code, 403, path)
            self.assertEqual(response.data["detail"], MSG_MUST_CHANGE_PASSWORD)
        response = client.post("/api/auth/change-password",
                               {"old_password": "Vaqtin4ik1", "new_password": "Yangi-parol-2026"}, format="json")
        self.assertEqual(response.status_code, 200, response.data)
        # The new password gives a normal session.
        client = APIClient()
        tokens = client.post("/api/auth/login", {"username": "yangi_logist", "password": "Yangi-parol-2026"},
                             format="json").data
        client.credentials(HTTP_AUTHORIZATION=f"Bearer {tokens['access']}")
        self.assertEqual(client.get("/api/logistics/tasks").status_code, 200)

    def test_bot_actions_refused_until_password_changed(self):
        client = APIClient()
        client.credentials(HTTP_X_BOT_TOKEN=BOT_API_TOKEN)
        self.assertEqual(client.get("/api/bot/users/700700").status_code, 200)  # profile shows the warning
        for path in ("/api/bot/users/700700/tasks", "/api/bot/users/700700/notifications",
                     "/api/bot/users/700700/summary"):
            response = client.get(path)
            self.assertEqual(response.status_code, 403, path)
            self.assertEqual(response.data["detail"], MSG_MUST_CHANGE_PASSWORD)
        self.fresh.must_change_password = False
        self.fresh.save(update_fields=["must_change_password"])
        self.assertEqual(client.get("/api/bot/users/700700/tasks").status_code, 200)

    def test_allow_list(self):
        self.assertTrue(temp_password_allows("POST", "/api/auth/logout"))
        self.assertTrue(temp_password_allows("GET", "/api/settings"))
        self.assertFalse(temp_password_allows("PATCH", "/api/settings"))
        self.assertFalse(temp_password_allows("GET", "/api/users"))

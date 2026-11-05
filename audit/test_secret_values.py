"""Verifier: no secret VALUE (passwords, temp passwords, JWTs, bot token) ends up anywhere in an audit row."""

from __future__ import annotations

import json

from django.core.cache import cache
from django.test import override_settings
from rest_framework.test import APITestCase

from accounts.models import User
from common.roles import Role
from common.testing import BOT_API_TOKEN, make_user

from .models import AuditLog


@override_settings(BOT_API_TOKEN=BOT_API_TOKEN)
class SecretValuesTests(APITestCase):
    def setUp(self):
        cache.clear()

    def test_no_secret_value_is_stored(self):
        secrets: list[str] = [BOT_API_TOKEN, "Admin-Parol-777"]
        make_user("bosh_admin", Role.ADMIN, password="Admin-Parol-777")
        login = self.client.post("/api/auth/login", {"username": "bosh_admin", "password": "Admin-Parol-777"},
                                 format="json").json()
        secrets += [login["access"], login["refresh"]]
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {login['access']}")
        created = self.client.post("/api/households", {
            "name": "Sir uy", "owner_first_name": "Ali", "owner_last_name": "Vali", "owner_username": "sir_owner",
            "phone": "+998901234567", "region": "Samarqand viloyati", "district": "Navobod tumani",
        }, format="json")
        self.assertEqual(created.status_code, 201, created.content)
        temp = created.json()["temp_password"]
        secrets.append(temp)
        owner_id = User.objects.get(username="sir_owner").pk
        reset = self.client.post(f"/api/users/{owner_id}/reset-password", format="json")
        self.assertEqual(reset.status_code, 200, reset.content)
        temp2 = reset.json()["temp_password"]
        secrets.append(temp2)
        # failed then successful owner login, password change, bot link with the new password
        self.client.credentials()
        self.client.post("/api/auth/login", {"username": "sir_owner", "password": "Wrong-Secret-1"}, format="json")
        secrets.append("Wrong-Secret-1")
        owner_login = self.client.post("/api/auth/login", {"username": "sir_owner", "password": temp2},
                                       format="json").json()
        secrets += [owner_login["access"], owner_login["refresh"]]
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {owner_login['access']}")
        changed = self.client.post("/api/auth/change-password",
                                   {"old_password": temp2, "new_password": "Yangi-Parol-2026"}, format="json")
        self.assertEqual(changed.status_code, 200, changed.content)
        secrets.append("Yangi-Parol-2026")
        self.client.credentials()
        linked = self.client.post("/api/bot/link", {"telegram_id": 555123, "username": "sir_owner",
                                                    "password": "Yangi-Parol-2026"},
                                  format="json", HTTP_X_BOT_TOKEN=BOT_API_TOKEN)
        self.assertEqual(linked.status_code, 200, linked.content)

        rows = list(AuditLog.objects.values())
        actions = {(row["module"], row["action"]) for row in rows}
        for expected in [("auth", "login_success"), ("auth", "login_failure"), ("auth", "password_change"),
                         ("users", "create"), ("users", "password_reset"), ("households", "create"),
                         ("telegram", "link")]:
            self.assertIn(expected, actions)
        dump = json.dumps(rows, default=str, ensure_ascii=False)
        for secret in secrets:
            self.assertNotIn(secret, dump)

"""Ending sessions also stops a Telegram initData re-login (security finding: end-sessions / logout all)."""

from __future__ import annotations

import time

from django.test import override_settings
from rest_framework.test import APIClient

from audit.models import AuditLog
from common.roles import Role
from common.testing import BOT_TOKEN, BaseAPITestCase, make_user

from .models import User
from .tests import signed_init_data

URL = "/api/auth/telegram"


@override_settings(BOT_TOKEN=BOT_TOKEN)
class TelegramReloginAfterRevocationTests(BaseAPITestCase):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.logist = make_user("logist_rev", Role.LOGISTICS, telegram_id=555001)

    def _telegram_login(self, init_data: str):
        return APIClient().post(URL, {"init_data": init_data}, format="json")

    def test_end_sessions_rejects_old_init_data_but_accepts_a_fresh_one(self):
        old = signed_init_data(555001, auth_date=int(time.time()) - 60)
        self.assertEqual(self._telegram_login(old).status_code, 200)

        self.login_as(self.admin)
        response = self.client.post(f"/api/users/{self.logist.pk}/end-sessions", {}, format="json")
        self.assertEqual(response.status_code, 200, response.data)
        self.logist.refresh_from_db()
        self.assertIsNotNone(self.logist.sessions_revoked_at)
        self.assertEqual(self.logist.telegram_id, 555001)  # still linked

        response = self._telegram_login(old)
        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.data["detail"], "Sessiya muddati tugagan. Ilovani yopib, qayta oching.")
        self.assertTrue(
            AuditLog.objects.filter(action="login_failure", actor=self.logist, after__reason="session_revoked").exists()
        )

        fresh = signed_init_data(555001, auth_date=int(time.time()) + 2)
        self.assertEqual(self._telegram_login(fresh).status_code, 200)

    def test_logout_all_rejects_old_init_data(self):
        old = signed_init_data(555001, auth_date=int(time.time()) - 5)
        self.login_as(self.logist)
        response = self.client.post("/api/auth/logout", {"all": True}, format="json")
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(self._telegram_login(old).status_code, 401)
        self.assertIsNotNone(User.objects.get(pk=self.logist.pk).sessions_revoked_at)

    def test_single_logout_keeps_telegram_login(self):
        old = signed_init_data(555001, auth_date=int(time.time()) - 5)
        tokens = self._telegram_login(old).data
        self.login_as(self.logist)
        response = self.client.post("/api/auth/logout", {"refresh": tokens["refresh"]}, format="json")
        self.assertEqual(response.status_code, 200, response.data)
        self.assertIsNone(User.objects.get(pk=self.logist.pk).sessions_revoked_at)
        self.assertEqual(self._telegram_login(old).status_code, 200)

"""Token lifecycle, blocked users, initData hardening and login-throttle client identification."""

from __future__ import annotations

from django.test import SimpleTestCase
from rest_framework_simplejwt.tokens import RefreshToken

from common.roles import Role
from common.testing import BOT_TOKEN, BaseAPITestCase, make_user

from .models import User
from .telegram import InitDataInvalid, validate_init_data
from .tokens import issue_tokens

MSG_BLOCKED = "Hisobingiz bloklangan. Administratorga murojaat qiling."


class TokenLifecycleTests(BaseAPITestCase):
    def test_blocked_user_access_token_is_rejected(self):
        tokens = issue_tokens(self.owner_a)
        User.objects.filter(pk=self.owner_a.pk).update(is_active=False)
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {tokens['access']}")
        response = self.client.get("/api/auth/me")
        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.data["detail"], MSG_BLOCKED)

    def test_blocked_user_cannot_refresh(self):
        tokens = issue_tokens(self.owner_a)
        User.objects.filter(pk=self.owner_a.pk).update(is_active=False)
        response = self.client.post("/api/auth/refresh", {"refresh": tokens["refresh"]}, format="json")
        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.data["detail"], MSG_BLOCKED)

    def test_refresh_of_deleted_user_is_401_not_500(self):
        user = make_user("temporary", Role.WAREHOUSE)
        token = RefreshToken.for_user(user)
        user.delete()
        response = self.client.post("/api/auth/refresh", {"refresh": str(token)}, format="json")
        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.data["detail"], "Sessiya muddati tugagan. Qaytadan kiring.")

    def test_garbage_refresh_token_is_401_in_uzbek(self):
        response = self.client.post("/api/auth/refresh", {"refresh": "not-a-token"}, format="json")
        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.data["detail"], "Sessiya muddati tugagan. Qaytadan kiring.")

    def test_access_token_cannot_be_used_as_refresh(self):
        access = RefreshToken.for_user(self.admin).access_token
        response = self.client.post("/api/auth/refresh", {"refresh": str(access)}, format="json")
        self.assertEqual(response.status_code, 401)

    def test_superuser_with_owner_role_acts_as_admin_everywhere(self):
        root = make_user("root", Role.OWNER, is_superuser=True, is_staff=True)
        self.login_as(root)
        self.assertEqual(self.client.get("/api/auth/me").data["role"], Role.ADMIN)
        self.assertEqual(self.client.get("/api/users").status_code, 200)
        self.assertEqual(self.client.get("/api/dashboard/admin").status_code, 200)
        self.assertEqual(self.client.get("/api/dashboard/owner").status_code, 403)
        self.assertEqual(self.client.get("/api/households").data["count"], 2)
        listed = self.client.get("/api/users?role=ADMIN").data["results"]
        self.assertIn("root", {row["username"] for row in listed})


class LoginThrottleIdentityTests(BaseAPITestCase):
    url = "/api/auth/login"

    def test_spoofed_forwarded_for_does_not_bypass_throttle(self):
        """Only the entry appended by the trusted proxy (the last one) identifies the client."""
        for index in range(10):
            self.client.post(
                self.url,
                {"username": "owner_a", "password": "xato"},
                format="json",
                HTTP_X_FORWARDED_FOR=f"10.0.0.{index}, 203.0.113.7",
            )
        response = self.client.post(
            self.url,
            {"username": "owner_a", "password": "xato"},
            format="json",
            HTTP_X_FORWARDED_FOR="10.9.9.9, 203.0.113.7",
        )
        self.assertEqual(response.status_code, 429)

    def test_different_clients_have_separate_buckets(self):
        for index in range(10):
            self.client.post(
                self.url, {"username": f"nobody{index}", "password": "xato"}, format="json",
                HTTP_X_FORWARDED_FOR="203.0.113.7",
            )
        response = self.client.post(
            self.url, {"username": "owner_a", "password": "Parol12345"}, format="json",
            HTTP_X_FORWARDED_FOR="203.0.113.8",
        )
        self.assertEqual(response.status_code, 200)

    def test_rotating_forwarded_for_cannot_bypass_username_limit(self):
        """A client that controls the whole header still gets 10 guesses per username a minute."""
        for index in range(10):
            self.client.post(
                self.url, {"username": "owner_a", "password": "xato"}, format="json",
                HTTP_X_FORWARDED_FOR=f"10.0.0.{index}",
            )
        response = self.client.post(
            self.url, {"username": " Owner_A ", "password": "Parol12345"}, format="json",
            HTTP_X_FORWARDED_FOR="10.0.1.1",
        )
        self.assertEqual(response.status_code, 429)
        other = self.client.post(
            self.url, {"username": "owner_b", "password": "Parol12345"}, format="json",
            HTTP_X_FORWARDED_FOR="10.0.1.2",
        )
        self.assertEqual(other.status_code, 200)


class InitDataHardeningTests(SimpleTestCase):
    def test_non_ascii_hash_is_invalid_not_a_crash(self):
        with self.assertRaises(InitDataInvalid):
            validate_init_data("auth_date=1&user=%7B%22id%22%3A1%7D&hash=%C3%A9", BOT_TOKEN)

    def test_duplicate_keys_are_rejected(self):
        with self.assertRaises(InitDataInvalid):
            validate_init_data("auth_date=1&auth_date=2&hash=abc", BOT_TOKEN)

    def test_malformed_query_string_is_rejected(self):
        with self.assertRaises(InitDataInvalid):
            validate_init_data("no-equals-sign", BOT_TOKEN)


class TelegramLoginHardeningTests(BaseAPITestCase):
    def test_non_ascii_hash_is_401(self):
        with self.settings(BOT_TOKEN=BOT_TOKEN):
            response = self.client.post(
                "/api/auth/telegram", {"init_data": "auth_date=1&hash=%C3%A9"}, format="json"
            )
        self.assertEqual(response.status_code, 401)

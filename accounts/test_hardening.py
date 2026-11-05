"""Session revocation, superuser protection, password strength and the staff-only user filter."""

from __future__ import annotations

from decimal import Decimal

from django.test import override_settings
from rest_framework_simplejwt.tokens import RefreshToken

from common.roles import Role
from common.testing import BOT_API_TOKEN, BaseAPITestCase, make_user
from farm import services as farm_services
from farm.models import Household

from .models import User

PASSWORD = "Parol12345"
REVOKED = "Sessiya muddati tugagan. Qaytadan kiring."


class SessionRevocationTests(BaseAPITestCase):
    def login(self, username: str, password: str = PASSWORD) -> dict:
        response = self.client.post("/api/auth/login", {"username": username, "password": password}, format="json")
        self.assertEqual(response.status_code, 200, response.data)
        return response.data

    def me(self, access: str) -> int:
        return self.client.get("/api/auth/me", HTTP_AUTHORIZATION=f"Bearer {access}").status_code

    def refresh(self, refresh: str):
        return self.client.post("/api/auth/refresh", {"refresh": refresh}, format="json")

    def assert_revoked(self, tokens: dict) -> None:
        self.assertEqual(self.me(tokens["access"]), 401)
        response = self.refresh(tokens["refresh"])
        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.data["detail"], REVOKED)

    def test_tokens_work_until_revoked(self):
        tokens = self.login("ombor1")
        self.assertEqual(self.me(tokens["access"]), 200)
        self.assertEqual(self.refresh(tokens["refresh"]).status_code, 200)

    def test_reset_password_revokes_existing_tokens(self):
        stolen = self.login("ombor1")
        self.login_as(self.admin)
        temp = self.client.post(f"/api/users/{self.warehouse.pk}/reset-password").data["temp_password"]
        self.client.force_authenticate(user=None)
        self.assert_revoked(stolen)
        fresh = self.login("ombor1", temp)
        self.assertEqual(self.me(fresh["access"]), 200)

    def test_change_password_revokes_existing_tokens(self):
        stolen = self.login("ombor1")
        response = self.client.post(
            "/api/auth/change-password",
            {"old_password": PASSWORD, "new_password": "Yangi-Parol-2026"},
            format="json",
            HTTP_AUTHORIZATION=f"Bearer {stolen['access']}",
        )
        self.assertEqual(response.status_code, 200)
        self.assert_revoked(stolen)
        self.assertEqual(self.me(self.login("ombor1", "Yangi-Parol-2026")["access"]), 200)

    @override_settings(BOT_API_TOKEN=BOT_API_TOKEN)
    def test_telegram_unlink_revokes_existing_tokens(self):
        User.objects.filter(pk=self.owner_a.pk).update(telegram_id=4242)
        stolen = self.login("owner_a")
        response = self.client.post(
            "/api/bot/unlink", {"telegram_id": 4242}, format="json", HTTP_X_BOT_TOKEN=BOT_API_TOKEN
        )
        self.assertEqual(response.status_code, 200)
        self.assert_revoked(stolen)

    @override_settings(BOT_API_TOKEN=BOT_API_TOKEN)
    def test_moving_a_telegram_link_revokes_the_previous_holder(self):
        User.objects.filter(pk=self.owner_a.pk).update(telegram_id=4242)
        previous = self.login("owner_a")
        untouched = self.login("owner_b")
        response = self.client.post(
            "/api/bot/link",
            {"telegram_id": 4242, "username": "owner_b", "password": PASSWORD},
            format="json",
            HTTP_X_BOT_TOKEN=BOT_API_TOKEN,
        )
        self.assertEqual(response.status_code, 200)
        self.assert_revoked(previous)
        self.assertEqual(self.me(untouched["access"]), 200)

    def test_admin_unlink_revokes_existing_tokens(self):
        User.objects.filter(pk=self.warehouse.pk).update(telegram_id=555)
        stolen = self.login("ombor1")
        self.login_as(self.admin)
        self.client.post(f"/api/users/{self.warehouse.pk}/unlink-telegram")
        self.client.force_authenticate(user=None)
        self.assert_revoked(stolen)

    def test_token_without_version_claim_is_rejected(self):
        legacy = RefreshToken.for_user(self.warehouse)
        self.assert_revoked({"access": str(legacy.access_token), "refresh": str(legacy)})


class SuperuserProtectionTests(BaseAPITestCase):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.root = make_user("root", Role.ADMIN, is_superuser=True, is_staff=True)

    def test_api_admin_cannot_see_or_touch_a_superuser(self):
        self.login_as(self.admin)
        listed = {row["username"] for row in self.client.get("/api/users").data["results"]}
        self.assertNotIn("root", listed)
        for method, url, data in (
            ("get", f"/api/users/{self.root.pk}", None),
            ("patch", f"/api/users/{self.root.pk}", {"is_active": False}),
            ("post", f"/api/users/{self.root.pk}/reset-password", None),
            ("post", f"/api/users/{self.root.pk}/unlink-telegram", None),
        ):
            response = getattr(self.client, method)(url, data, format="json")
            self.assertEqual(response.status_code, 404, (method, url))
        self.root.refresh_from_db()
        self.assertTrue(self.root.is_active)
        self.assertTrue(self.root.check_password(PASSWORD))

    def test_superuser_manages_everyone(self):
        other_root = make_user("root2", Role.ADMIN, is_superuser=True, is_staff=True)
        self.login_as(self.root)
        listed = {row["username"] for row in self.client.get("/api/users").data["results"]}
        self.assertTrue({"root", "root2", "admin1"} <= listed)
        response = self.client.post(f"/api/users/{other_root.pk}/reset-password")
        self.assertEqual(response.status_code, 200)


class PasswordStrengthTests(BaseAPITestCase):
    url = "/api/auth/change-password"

    def change(self, new_password: str):
        self.login_as(self.warehouse)
        return self.client.post(self.url, {"old_password": PASSWORD, "new_password": new_password}, format="json")

    def test_weak_passwords_are_rejected_in_uzbek(self):
        cases = {
            "12345678": "Parol faqat raqamlardan iborat bo‘lmasligi kerak",
            "password": "Bu parol juda oddiy. Harflar va raqamlardan iborat murakkabroq parol tanlang",
            "ombor1ombor": "Parol login yoki ism-familiyangizga juda o‘xshash",
            "qisqa1": "Parol kamida 8 ta belgidan iborat bo‘lishi kerak",
        }
        for password, message in cases.items():
            response = self.change(password)
            self.assertEqual(response.status_code, 400, password)
            self.assertIn(message, response.data["new_password"], password)

    def test_strong_password_is_accepted(self):
        self.assertEqual(self.change("Navobod-Yem-2026").status_code, 200)


class StaffFilterTests(BaseAPITestCase):
    def test_comma_separated_roles_list_staff_only(self):
        root = make_user("root", Role.OWNER, is_superuser=True, is_staff=True)
        self.login_as(root)
        response = self.client.get("/api/users?role=ADMIN,WAREHOUSE")
        self.assertEqual(response.status_code, 200)
        usernames = {row["username"] for row in response.data["results"]}
        self.assertEqual(usernames, {"admin1", "ombor1", "root"})
        self.assertEqual(self.client.get("/api/users?role=ADMIN,BAD").status_code, 400)


class DashboardReportCountTests(BaseAPITestCase):
    def test_reports_of_deactivated_households_are_not_counted(self):
        farm_services.create_daily_report(actor=self.owner_a, household=None, feed_kg=Decimal("1"), meat_kg=Decimal("0.5"))
        Household.objects.filter(pk=self.household_a.pk).update(is_active=False)
        self.login_as(self.admin)
        data = self.client.get("/api/dashboard/admin").data
        self.assertEqual((data["reports_today"], data["households_active"]), (0, 1))

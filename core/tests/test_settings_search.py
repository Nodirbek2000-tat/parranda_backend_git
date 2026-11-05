"""Company settings (V3 §3), global search (V3 §12) and the scaffold wiring (V3 §0.2)."""

from __future__ import annotations

from django.apps import apps
from django.urls import resolve

from accounts.models import User
from audit.models import AuditLog
from common.roles import Role
from common.testing import BaseAPITestCase, make_user
from core.models import SystemSettings
from core.settings_service import get_settings


class ScaffoldWiringTests(BaseAPITestCase):
    def test_v3_apps_installed(self):
        for label in ("core", "orders", "logistics", "vet", "reports", "token_blacklist"):
            self.assertTrue(apps.is_installed(label) or apps.get_app_config(label), label)

    def test_core_routes_resolve(self):
        for path in ("/api/settings", "/api/files", "/api/files/1", "/api/files/1/meta", "/api/search",
                     "/api/my/notifications", "/api/notifications", "/api/notification-settings",
                     "/api/auth/logout", "/api/users/1/end-sessions", "/api/bot/reminders/run"):
            resolve(path)

    def test_singleton_created_by_migration(self):
        self.assertTrue(SystemSettings.objects.filter(pk=1).exists())
        self.assertEqual(get_settings().short_name, "NNP")


class SettingsApiTests(BaseAPITestCase):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.root = make_user("root1", Role.ADMIN, is_superuser=True, is_staff=True)
        cls.vet = make_user("vet1", Role.VET)

    def test_get_for_every_role(self):
        for user in (self.admin, self.warehouse, self.owner_a, self.vet, self.root):
            self.login_as(user)
            response = self.client.get("/api/settings")
            self.assertEqual(response.status_code, 200, user.username)
            self.assertEqual(response.data["company_name"], "Navobod naslli parranda qisqarmasi")
            self.assertEqual(response.data["reminder_hour"], 8)
            self.assertEqual(response.data["mortality_alert_threshold_pct"], 5.0)

    def test_patch_super_admin_only(self):
        self.login_as(self.admin)
        response = self.client.patch("/api/settings", {"short_name": "X"}, format="json")
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.data["detail"], "Bu amal faqat Super Admin uchun.")
        self.login_as(self.owner_a)
        self.assertEqual(self.client.patch("/api/settings", {"short_name": "X"}, format="json").status_code, 403)

    def test_patch_updates_and_audits_changed_fields(self):
        self.login_as(self.root)
        response = self.client.patch(
            "/api/settings", {"phone": "+998901112233", "reminder_hour": 9, "company_name": "NNP MChJ"}, format="json"
        )
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["reminder_hour"], 9)
        self.assertEqual(response.data["updated_by"], {"id": self.root.pk, "full_name": self.root.full_name})
        log = AuditLog.objects.get(module="settings", action="update")
        self.assertEqual(log.object_type, "SystemSettings")
        self.assertEqual(log.after, {"phone": "+998901112233", "reminder_hour": 9, "company_name": "NNP MChJ"})
        self.assertEqual(log.before["reminder_hour"], 8)
        # unchanged values → no second audit row
        self.client.patch("/api/settings", {"reminder_hour": 9}, format="json")
        self.assertEqual(AuditLog.objects.filter(module="settings").count(), 1)

    def test_validation_messages(self):
        self.login_as(self.root)
        cases = [
            ({"vaccination_reminder_days": 31}, "vaccination_reminder_days", "0 dan 30 gacha butun son kiriting."),
            ({"expiry_reminder_days": -1}, "expiry_reminder_days", "0 dan 365 gacha butun son kiriting."),
            ({"problem_report_days": 0}, "problem_report_days", "1 dan 30 gacha butun son kiriting."),
            ({"reminder_hour": 24}, "reminder_hour", "0 dan 23 gacha butun son kiriting."),
            ({"reminder_hour": "abc"}, "reminder_hour", "0 dan 23 gacha butun son kiriting."),
            ({"company_name": "A"}, "company_name", "Kamida 2 ta belgi kiriting."),
            ({"last_reminders_date": "2026-01-01"}, "last_reminders_date", "Bu maydonni o‘zgartirib bo‘lmaydi."),
        ]
        for payload, field, message in cases:
            response = self.client.patch("/api/settings", payload, format="json")
            self.assertEqual(response.status_code, 400, payload)
            self.assertEqual(response.data[field], [message], payload)
        response = self.client.patch("/api/settings", {"mortality_alert_threshold_pct": 100.5}, format="json")
        self.assertEqual(response.status_code, 400)
        self.assertEqual(AuditLog.objects.filter(module="settings").count(), 0)


class SearchTests(BaseAPITestCase):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.logistics = make_user("logist1", Role.LOGISTICS)

    def test_query_length(self):
        self.login_as(self.admin)
        response = self.client.get("/api/search?q=a")
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.data["q"], ["Kamida 2 ta belgi kiriting."])

    def test_admin_finds_household_by_code_and_name(self):
        self.login_as(self.admin)
        response = self.client.get(f"/api/search?q={self.household_a.code}")
        self.assertEqual(response.status_code, 200)
        group = response.data["groups"][0]
        self.assertEqual(group["type"], "household")
        self.assertEqual(group["items"][0]["path"], f"/households/{self.household_a.pk}")
        response = self.client.get("/api/search?q=owner_")
        self.assertIn("user", [g["type"] for g in response.data["groups"]])

    def test_owner_sees_only_own_household(self):
        self.login_as(self.owner_a)
        response = self.client.get("/api/search?q=uy")
        households = [g for g in response.data["groups"] if g["type"] == "household"][0]["items"]
        self.assertEqual([item["id"] for item in households], [self.household_a.pk])
        self.assertEqual(households[0]["path"], "/my")
        self.assertNotIn("user", [g["type"] for g in response.data["groups"]])

    def test_logistics_sees_no_phase1_groups(self):
        self.login_as(self.logistics)
        response = self.client.get("/api/search?q=uy")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["groups"], [])

    def test_superusers_hidden_from_plain_admin(self):
        make_user("rootsearch", Role.ADMIN, is_superuser=True)
        self.login_as(self.admin)
        response = self.client.get("/api/search?q=rootsearch")
        self.assertEqual(response.data["groups"], [])
        self.assertTrue(User.objects.filter(username="rootsearch").exists())


class AdminReadOnlyTests(BaseAPITestCase):
    """Django admin cannot bypass the audited Web App path for settings and the fleet (security finding)."""

    def test_settings_vehicle_driver_admin_are_read_only(self):
        from django.contrib import admin
        from django.test import RequestFactory

        from core.models import SystemSettings
        from logistics.models import Driver, Vehicle

        root = make_user("root_admin_ro", Role.ADMIN, is_superuser=True, is_staff=True)
        request = RequestFactory().get("/admin/")
        request.user = root
        for model in (SystemSettings, Vehicle, Driver):
            model_admin = admin.site._registry[model]
            self.assertFalse(model_admin.has_add_permission(request), model)
            self.assertFalse(model_admin.has_change_permission(request), model)
            self.assertFalse(model_admin.has_delete_permission(request), model)
            self.assertTrue(model_admin.has_view_permission(request), model)

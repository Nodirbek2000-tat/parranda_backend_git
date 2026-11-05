"""V3 roles, user object, logout, end-sessions (API_CONTRACT_V3 §1, §2)."""

from __future__ import annotations

from audit.models import AuditLog
from common.roles import Role
from common.testing import BaseAPITestCase, make_user
from notifications.models import Notification, NotificationStatus

from .models import User


class V3RoleTests(BaseAPITestCase):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.root = make_user("root1", Role.ADMIN, is_superuser=True, is_staff=True)
        cls.logistics = make_user("logist1", Role.LOGISTICS)
        cls.vet = make_user("vet1", Role.VET)

    def test_user_object_fields(self):
        self.login_as(self.logistics)
        data = self.client.get("/api/auth/me").data
        self.assertEqual(data["role"], "LOGISTICS")
        self.assertEqual(data["role_display"], "Logistika xodimi")
        self.assertFalse(data["is_super_admin"])
        self.assertIsNone(data["driver_id"])
        self.login_as(self.root)
        data = self.client.get("/api/auth/me").data
        self.assertEqual((data["role"], data["role_display"], data["is_super_admin"]), ("ADMIN", "Super Admin", True))
        self.login_as(self.vet)
        self.assertEqual(self.client.get("/api/auth/me").data["role_display"], "Veterinar")

    def test_create_logistics_and_vet_users(self):
        self.login_as(self.admin)
        for role, display in (("LOGISTICS", "Logistika xodimi"), ("VET", "Veterinar")):
            with self.captureOnCommitCallbacks(execute=True):
                response = self.client.post(
                    "/api/users",
                    {"username": f"new_{role.lower()}", "first_name": "Jasur", "phone": "+998901112233", "role": role},
                    format="json",
                )
            self.assertEqual(response.status_code, 201, response.data)
            self.assertEqual(response.data["user"]["role_display"], display)
            new_id = response.data["user"]["id"]
            rows = Notification.objects.filter(event="user_created", object_id=new_id)
            recipients = set(rows.values_list("recipient_id", flat=True))
            self.assertIn(new_id, recipients)  # subject_user
            self.assertIn(self.admin.pk, recipients)
            self.assertIn(self.root.pk, recipients)  # superusers count as ADMIN
            self.assertTrue(all(row.status == NotificationStatus.SKIPPED for row in rows))  # nobody linked
            self.assertEqual(rows.get(recipient_id=new_id).button_path, "/")
        response = self.client.post(
            "/api/users", {"username": "bad", "first_name": "X", "role": "OWNER"}, format="json"
        )
        self.assertEqual(response.data, {"role": ["“OWNER” ruxsat etilgan qiymat emas."]})

    def test_owner_creation_notifies_only_the_owner_with_user_created(self):
        self.login_as(self.admin)
        with self.captureOnCommitCallbacks(execute=True):
            response = self.client.post(
                "/api/households",
                {"name": "Yangi uy", "owner_first_name": "A", "owner_last_name": "B", "owner_username": "yangi1",
                 "phone": "+998901234567", "region": "Samarqand viloyati", "district": "Navobod tumani"},
                format="json",
            )
        owner_id = User.objects.get(username="yangi1").pk
        self.assertEqual(response.status_code, 201)
        rows = Notification.objects.filter(event="user_created")
        self.assertEqual(list(rows.values_list("recipient_id", flat=True)), [owner_id])

    def test_role_filter_accepts_five_roles(self):
        self.login_as(self.admin)
        response = self.client.get("/api/users?role=LOGISTICS,VET")
        self.assertEqual({row["username"] for row in response.data["results"]}, {"logist1", "vet1"})

    def test_new_roles_cannot_use_phase1_endpoints(self):
        staff_only = ("/api/stock-transactions", "/api/users", "/api/dashboard/admin", "/api/audit-logs",
                      "/api/notifications")
        for user in (self.logistics, self.vet):
            self.login_as(user)
            for path in staff_only:
                self.assertEqual(self.client.get(path).status_code, 403, (user.username, path))
        # V3 §1.2 / §18.3: VET reads households, batches and products; LOGISTICS never does.
        for path in ("/api/households", "/api/batches", "/api/products"):
            self.login_as(self.logistics)
            self.assertEqual(self.client.get(path).status_code, 403, path)
            self.login_as(self.vet)
            self.assertEqual(self.client.get(path).status_code, 200, path)


class LogoutTests(BaseAPITestCase):
    def login(self, username: str = "ombor1") -> dict:
        response = self.client.post("/api/auth/login", {"username": username, "password": "Parol12345"}, format="json")
        self.assertEqual(response.status_code, 200, response.data)
        return response.data

    def auth(self, tokens: dict) -> None:
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {tokens['access']}")

    def test_logout_blacklists_refresh(self):
        tokens = self.login()
        self.auth(tokens)
        response = self.client.post("/api/auth/logout", {"refresh": tokens["refresh"]}, format="json")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data, {"detail": "Tizimdan chiqildi"})
        refreshed = self.client.post("/api/auth/refresh", {"refresh": tokens["refresh"]}, format="json")
        self.assertEqual(refreshed.status_code, 401)
        self.assertEqual(
            refreshed.data, {"detail": "Sessiya muddati tugagan. Qaytadan kiring.", "code": "token_not_valid"}
        )
        again = self.client.post("/api/auth/logout", {"refresh": tokens["refresh"]}, format="json")
        self.assertEqual(again.status_code, 200)  # idempotent
        log = AuditLog.objects.filter(module="auth", action="logout").first()
        self.assertEqual(log.after, {"all": False})
        self.assertEqual(log.actor_id, self.warehouse.pk)

    def test_logout_rejects_bad_or_foreign_token(self):
        own = self.login()
        other = self.login("admin1")
        self.auth(own)
        for value in ("garbage", other["refresh"], own["access"], ""):
            response = self.client.post("/api/auth/logout", {"refresh": value}, format="json")
            self.assertEqual(response.status_code, 400, value[:10])
            self.assertEqual(response.data, {"refresh": ["Token yaroqsiz."]})
        self.assertEqual(self.client.post("/api/auth/refresh", {"refresh": other["refresh"]}, format="json")
                         .status_code, 200)

    def test_logout_all_revokes_every_session(self):
        first = self.login()
        second = self.login()
        self.auth(first)
        response = self.client.post("/api/auth/logout", {"all": True}, format="json")
        self.assertEqual(response.status_code, 200)
        self.auth(second)
        self.assertEqual(self.client.get("/api/auth/me").status_code, 401)
        self.assertEqual(self.client.post("/api/auth/refresh", {"refresh": second["refresh"]}, format="json")
                         .status_code, 401)
        self.assertEqual(AuditLog.objects.get(module="auth", action="logout").after, {"all": True})

    def test_new_roles_can_log_out(self):
        make_user("vet1", Role.VET)
        tokens = self.login("vet1")
        self.auth(tokens)
        self.assertEqual(self.client.post("/api/auth/logout", {"refresh": tokens["refresh"]}, format="json")
                         .status_code, 200)


class EndSessionsTests(BaseAPITestCase):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.root = make_user("root1", Role.ADMIN, is_superuser=True, is_staff=True)

    def test_end_sessions_bumps_version_and_optionally_unlinks(self):
        User.objects.filter(pk=self.warehouse.pk).update(telegram_id=5005)
        self.login_as(self.admin)
        before = User.objects.get(pk=self.warehouse.pk).token_version
        response = self.client.post(f"/api/users/{self.warehouse.pk}/end-sessions", {}, format="json")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data, {"detail": "Barcha sessiyalar yakunlandi"})
        target = User.objects.get(pk=self.warehouse.pk)
        self.assertEqual(target.token_version, before + 1)
        self.assertEqual(target.telegram_id, 5005)
        response = self.client.post(
            f"/api/users/{self.warehouse.pk}/end-sessions", {"unlink_telegram": True}, format="json"
        )
        self.assertEqual(response.status_code, 200)
        self.assertIsNone(User.objects.get(pk=self.warehouse.pk).telegram_id)
        logs = AuditLog.objects.filter(module="users", action="sessions_revoked").order_by("id")
        self.assertEqual([log.after for log in logs], [{"unlink_telegram": False}, {"unlink_telegram": True}])
        self.assertTrue(AuditLog.objects.filter(module="telegram", action="unlink").exists())

    def test_rules(self):
        self.login_as(self.admin)
        response = self.client.post(f"/api/users/{self.admin.pk}/end-sessions", {}, format="json")
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.data, {"detail": "O‘z sessiyalaringizni “Chiqish” orqali yakunlang."})
        self.assertEqual(self.client.post(f"/api/users/{self.root.pk}/end-sessions", {}, format="json").status_code,
                         404)
        self.login_as(self.warehouse)
        self.assertEqual(self.client.post(f"/api/users/{self.admin.pk}/end-sessions", {}, format="json").status_code,
                         403)

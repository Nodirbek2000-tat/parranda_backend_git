import json
import time
from urllib.parse import urlencode

from django.test import override_settings
from rest_framework.test import APIClient

from accounts.models import Role, User
from accounts.telegram import build_data_check_string, sign
from audit.models import AuditLog
from config.testutils import NNPTestCase

BOT_TOKEN = "123:test-bot-token"


def init_data(telegram_id: int, *, token=BOT_TOKEN, auth_date=None) -> str:
    fields = {"auth_date": str(auth_date or int(time.time())), "query_id": "AAE",
              "user": json.dumps({"id": telegram_id, "first_name": "Ali"})}
    fields["hash"] = sign(build_data_check_string(fields), token)
    return urlencode(fields)


class LoginTests(NNPTestCase):
    def login(self, username, password):
        return APIClient().post("/api/auth/login", {"username": username, "password": password}, format="json")

    def test_login_shape_and_me(self):
        r = self.login("inspector", "Parol-12345")
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertEqual(set(body), {"access", "refresh", "user"})
        for key in ("id", "username", "full_name", "role", "role_display", "is_superadmin", "must_change_password"):
            self.assertIn(key, body["user"])
        self.assertEqual(body["user"]["role_display"], "Inspektor")
        client = APIClient()
        client.credentials(HTTP_AUTHORIZATION=f"Bearer {body['access']}")
        self.assertEqual(client.get("/api/auth/me").json()["username"], "inspector")
        r = APIClient().post("/api/auth/refresh", {"refresh": body["refresh"]}, format="json")
        self.assertEqual(r.status_code, 200)
        self.assertIn("access", r.json())

    def test_bad_password_and_blocked(self):
        r = self.login("inspector", "wrong")
        self.assertEqual(r.status_code, 401)
        self.assertEqual(r.json(), {"detail": "Login yoki parol noto‘g‘ri"})
        User.objects.filter(username="inspector").update(is_active=False)
        self.assertEqual(self.login("inspector", "Parol-12345").status_code, 401)

    def test_temporary_password_gate_and_change(self):
        User.objects.filter(username="hr").update(must_change_password=True)
        tokens = self.login("hr", "Parol-12345").json()
        client = APIClient()
        client.credentials(HTTP_AUTHORIZATION=f"Bearer {tokens['access']}")
        self.assertEqual(client.get("/api/cases").status_code, 403)
        self.assertEqual(client.get("/api/auth/me").status_code, 200)
        r = client.post("/api/auth/change-password", {"old_password": "bad", "new_password": "Yangi-parol-1"},
                        format="json")
        self.assertEqual(r.status_code, 400)
        r = client.post("/api/auth/change-password", {"old_password": "Parol-12345", "new_password": "Yangi-parol-1"},
                        format="json")
        self.assertEqual(r.status_code, 200)
        self.assertFalse(r.json()["user"]["must_change_password"])
        # old tokens are revoked (token_version), new ones work
        self.assertEqual(client.get("/api/auth/me").status_code, 401)
        refresh = APIClient().post("/api/auth/refresh", {"refresh": tokens["refresh"]}, format="json")
        self.assertEqual(refresh.status_code, 401)
        client.credentials(HTTP_AUTHORIZATION=f"Bearer {r.json()['access']}")
        self.assertEqual(client.get("/api/cases").status_code, 200)

    def test_superuser_is_superadmin(self):
        user = User.objects.create_superuser("root", "r@x.uz", "Parol-12345")
        self.assertEqual(user.role, Role.SUPERADMIN)
        self.assertTrue(self.login("root", "Parol-12345").json()["user"]["is_superadmin"])


class TelegramAuthTests(NNPTestCase):
    def test_valid_hmac_logs_in_linked_user(self):
        User.objects.filter(username="vet").update(telegram_id=555)
        r = APIClient().post("/api/auth/telegram", {"init_data": init_data(555)}, format="json")
        self.assertEqual(r.status_code, 200, r.content)
        self.assertEqual(r.json()["user"]["username"], "vet")
        self.assertIn("access", r.json())

    def test_invalid_hmac_expired_and_unlinked(self):
        User.objects.filter(username="vet").update(telegram_id=555)
        bad = init_data(555, token="999:other")
        self.assertEqual(APIClient().post("/api/auth/telegram", {"init_data": bad}, format="json").status_code, 401)
        tampered = init_data(555).replace("555", "556")
        self.assertEqual(APIClient().post("/api/auth/telegram", {"init_data": tampered}, format="json").status_code,
                         401)
        old = init_data(555, auth_date=int(time.time()) - 2 * 86400)
        self.assertEqual(APIClient().post("/api/auth/telegram", {"init_data": old}, format="json").status_code, 401)
        self.assertEqual(APIClient().post("/api/auth/telegram", {"init_data": ""}, format="json").status_code, 401)
        r = APIClient().post("/api/auth/telegram", {"init_data": init_data(777)}, format="json")
        self.assertEqual(r.status_code, 404)

    @override_settings(BOT_TOKEN="")
    def test_disabled_without_bot_token(self):
        User.objects.filter(username="vet").update(telegram_id=555)
        r = APIClient().post("/api/auth/telegram", {"init_data": init_data(555)}, format="json")
        self.assertEqual(r.status_code, 401)


class UserAdminTests(NNPTestCase):
    def test_admin_creates_staff_with_temp_password(self):
        r = self.client_for(Role.ADMIN).post(
            "/api/users", {"username": "vet2", "full_name": "Vet Ikki", "phone": "+998", "role": "VET"},
            format="json")
        self.assertEqual(r.status_code, 201, r.content)
        body = r.json()
        self.assertTrue(body["temp_password"])
        self.assertTrue(body["must_change_password"])
        self.assertTrue(User.objects.get(username="vet2").check_password(body["temp_password"]))
        self.assertTrue(AuditLog.objects.filter(action="user.create").exists())
        dup = self.client_for(Role.ADMIN).post("/api/users", {"username": "VET2", "full_name": "x", "role": "VET"},
                                               format="json")
        self.assertEqual(dup.status_code, 400)

    def test_role_rules(self):
        self.assertEqual(self.client_for(Role.HR).get("/api/users").status_code, 403)
        self.assertEqual(self.client_for(Role.ADMIN).get("/api/users").status_code, 200)
        r = self.client_for(Role.ADMIN).post("/api/users", {"username": "a2", "full_name": "x", "role": "ADMIN"},
                                             format="json")
        self.assertEqual(r.status_code, 403)
        r = self.client_for(Role.SUPERADMIN).post("/api/users", {"username": "a2", "full_name": "x", "role": "ADMIN"},
                                                  format="json")
        self.assertEqual(r.status_code, 201)

    def test_patch_reset_delete(self):
        vet = self.users[Role.VET]
        admin = self.client_for(Role.ADMIN)
        r = admin.patch(f"/api/users/{vet.pk}", {"role": "LOGISTICS", "full_name": "Yangi", "is_active": False},
                        format="json")
        self.assertEqual(r.status_code, 200)
        self.assertEqual((r.json()["role"], r.json()["is_active"]), ("LOGISTICS", False))
        version = User.objects.get(pk=vet.pk).token_version
        r = admin.post(f"/api/users/{vet.pk}/reset-password")
        self.assertTrue(r.json()["temp_password"])
        self.assertEqual(User.objects.get(pk=vet.pk).token_version, version + 1)
        self.assertEqual(admin.delete(f"/api/users/{self.users[Role.ADMIN].pk}").status_code, 400)  # own account
        self.assertEqual(admin.delete(f"/api/users/{vet.pk}").status_code, 204)
        self.assertNotIn(vet.pk, [u["id"] for u in admin.get("/api/users").json()])
        self.assertEqual(admin.get(f"/api/users/{vet.pk}").status_code, 404)

    def test_last_superadmin_protected(self):
        sa = self.users[Role.SUPERADMIN]
        other = User.objects.create_user(username="sa2", password="x", role=Role.SUPERADMIN)
        client = self.client_for(other)
        self.assertEqual(client.delete(f"/api/users/{sa.pk}").status_code, 204)
        self.assertEqual(client.delete(f"/api/users/{other.pk}").status_code, 400)  # self
        r = self.client_for(other).patch(f"/api/users/{other.pk}", {"role": "ADMIN"}, format="json")
        self.assertEqual(r.status_code, 400)
        admin = self.client_for(Role.ADMIN)
        self.assertEqual(admin.delete(f"/api/users/{other.pk}").status_code, 403)

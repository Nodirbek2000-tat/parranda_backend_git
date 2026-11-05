from __future__ import annotations

import json
import time
from urllib.parse import urlencode

from django.test import SimpleTestCase, override_settings

from common.exceptions import translate_message
from common.formatting import fmt_number
from common.roles import Role
from common.testing import BOT_TOKEN, BaseAPITestCase, make_user

from .models import User
from .telegram import InitDataInvalid, build_data_check_string, sign, validate_init_data


def signed_init_data(telegram_id: int, *, bot_token: str = BOT_TOKEN, auth_date: int | None = None) -> str:
    fields = {
        "query_id": "AAHdF6IQAAAAAN0XohDhrOrc",
        "user": json.dumps({"id": telegram_id, "first_name": "Ali", "username": "ali"}, separators=(",", ":")),
        "auth_date": str(int(time.time()) if auth_date is None else auth_date),
    }
    fields["hash"] = sign(build_data_check_string(fields), bot_token)
    return urlencode(fields)


class InitDataUnitTests(SimpleTestCase):
    def test_valid_payload_returns_user_id(self):
        self.assertEqual(validate_init_data(signed_init_data(777), BOT_TOKEN), 777)

    def test_tampered_payload_is_rejected(self):
        original = signed_init_data(777)
        data = original.replace("%3A777%2C", "%3A778%2C")
        self.assertNotEqual(original, data)
        with self.assertRaises(InitDataInvalid):
            validate_init_data(data, BOT_TOKEN)

    def test_wrong_bot_token_is_rejected(self):
        with self.assertRaises(InitDataInvalid):
            validate_init_data(signed_init_data(777, bot_token="other:token"), BOT_TOKEN)

    def test_expired_payload_is_rejected(self):
        old = int(time.time()) - 86_401
        with self.assertRaises(InitDataInvalid):
            validate_init_data(signed_init_data(777, auth_date=old), BOT_TOKEN)

    def test_missing_hash_and_empty_token(self):
        with self.assertRaises(InitDataInvalid):
            validate_init_data("auth_date=1&user=%7B%7D", BOT_TOKEN)
        with self.assertRaises(InitDataInvalid):
            validate_init_data(signed_init_data(777), "")


class CommonHelpersTests(SimpleTestCase):
    def test_number_format(self):
        self.assertEqual(fmt_number(1500), "1 500")
        self.assertEqual(fmt_number(1234567), "1 234 567")
        self.assertEqual(fmt_number("310.50"), "310.5")
        self.assertEqual(fmt_number(0), "0")

    def test_drf_messages_are_translated(self):
        self.assertEqual(translate_message("This field is required."), "Bu maydon to‘ldirilishi shart.")
        self.assertEqual(translate_message("No Household matches the given query."), "Topilmadi.")
        self.assertEqual(translate_message("Allaqachon o‘zbekcha"), "Allaqachon o‘zbekcha")


class LoginTests(BaseAPITestCase):
    url = "/api/auth/login"

    def test_owner_logs_in(self):
        response = self.client.post(self.url, {"username": "owner_a", "password": "Parol12345"}, format="json")
        self.assertEqual(response.status_code, 200)
        self.assertIn("access", response.data)
        self.assertIn("refresh", response.data)
        self.assertEqual(response.data["user"]["role"], Role.OWNER)
        self.assertEqual(response.data["user"]["household_id"], self.household_a.pk)

        access = response.data["access"]
        me = self.client.get("/api/auth/me", HTTP_AUTHORIZATION=f"Bearer {access}")
        self.assertEqual(me.status_code, 200)
        self.assertEqual(me.data["username"], "owner_a")

    def test_wrong_password_is_401(self):
        response = self.client.post(self.url, {"username": "owner_a", "password": "xato"}, format="json")
        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.data["detail"], "Login yoki parol noto‘g‘ri")

    def test_unknown_user_is_401(self):
        response = self.client.post(self.url, {"username": "nobody", "password": "xato"}, format="json")
        self.assertEqual(response.status_code, 401)

    def test_blocked_user_is_403(self):
        User.objects.filter(pk=self.owner_a.pk).update(is_active=False)
        response = self.client.post(self.url, {"username": "owner_a", "password": "Parol12345"}, format="json")
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.data["detail"], "Hisobingiz bloklangan. Administratorga murojaat qiling.")

    def test_superuser_is_reported_as_admin(self):
        make_user("root", Role.OWNER, is_superuser=True, is_staff=True)
        response = self.client.post(self.url, {"username": "root", "password": "Parol12345"}, format="json")
        self.assertEqual(response.data["user"]["role"], Role.ADMIN)
        self.assertIsNone(response.data["user"]["household_id"])

    def test_refresh_returns_access(self):
        tokens = self.client.post(self.url, {"username": "admin1", "password": "Parol12345"}, format="json").data
        response = self.client.post("/api/auth/refresh", {"refresh": tokens["refresh"]}, format="json")
        self.assertEqual(response.status_code, 200)
        self.assertIn("access", response.data)

    def test_login_is_throttled(self):
        for _ in range(10):
            self.client.post(self.url, {"username": "owner_a", "password": "xato"}, format="json")
        response = self.client.post(self.url, {"username": "owner_a", "password": "xato"}, format="json")
        self.assertEqual(response.status_code, 429)

    def test_trailing_slash_is_not_routed(self):
        response = self.client.post(f"{self.url}/", {"username": "owner_a", "password": "x"}, format="json")
        self.assertEqual(response.status_code, 404)

    def test_missing_fields_have_uzbek_messages(self):
        response = self.client.post(self.url, {}, format="json")
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.data["username"], ["Bu maydon to‘ldirilishi shart."])


class ChangePasswordTests(BaseAPITestCase):
    url = "/api/auth/change-password"

    def test_change_password(self):
        User.objects.filter(pk=self.owner_a.pk).update(must_change_password=True)
        self.login_as(self.owner_a)
        response = self.client.post(
            self.url, {"old_password": "Parol12345", "new_password": "YangiParol2026"}, format="json"
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["detail"], "Parol yangilandi")
        self.owner_a.refresh_from_db()
        self.assertFalse(self.owner_a.must_change_password)
        self.assertTrue(self.owner_a.check_password("YangiParol2026"))

    def test_rules(self):
        self.login_as(self.owner_a)
        short = self.client.post(self.url, {"old_password": "Parol12345", "new_password": "qisqa"}, format="json")
        self.assertEqual(short.status_code, 400)
        self.assertIn("new_password", short.data)
        same = self.client.post(
            self.url, {"old_password": "Parol12345", "new_password": "Parol12345"}, format="json"
        )
        self.assertEqual(same.status_code, 400)
        wrong = self.client.post(self.url, {"old_password": "xato", "new_password": "YangiParol2026"}, format="json")
        self.assertEqual(wrong.status_code, 400)
        self.assertIn("old_password", wrong.data)


class UserManagementTests(BaseAPITestCase):
    def test_admin_creates_staff_user(self):
        self.login_as(self.admin)
        response = self.client.post(
            "/api/users",
            {"username": "ombor2", "first_name": "Dilshod", "last_name": "Karimov", "phone": "+998901112233",
             "role": "WAREHOUSE"},
            format="json",
        )
        self.assertEqual(response.status_code, 201)
        password = response.data["temp_password"]
        self.assertEqual(len(password), 10)
        self.assertTrue(password.isalnum())
        self.assertTrue(response.data["user"]["must_change_password"])
        self.assertEqual(response.data["user"]["role"], "WAREHOUSE")
        self.assertTrue(User.objects.get(username="ombor2").check_password(password))

    def test_owner_role_cannot_be_created_directly(self):
        self.login_as(self.admin)
        response = self.client.post(
            "/api/users", {"username": "x_owner", "first_name": "X", "role": "OWNER"}, format="json"
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn("role", response.data)

    def test_non_admin_is_forbidden(self):
        self.login_as(self.warehouse)
        self.assertEqual(self.client.get("/api/users").status_code, 403)
        self.login_as(self.owner_a)
        self.assertEqual(self.client.get("/api/users").status_code, 403)

    def test_anonymous_is_401(self):
        self.assertEqual(self.client.get("/api/users").status_code, 401)

    def test_admin_cannot_block_himself(self):
        self.login_as(self.admin)
        response = self.client.patch(f"/api/users/{self.admin.pk}", {"is_active": False}, format="json")
        self.assertEqual(response.status_code, 400)
        response = self.client.patch(f"/api/users/{self.warehouse.pk}", {"is_active": False}, format="json")
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.data["is_active"])

    def test_reset_password_and_unlink(self):
        User.objects.filter(pk=self.warehouse.pk).update(telegram_id=555)
        self.login_as(self.admin)
        response = self.client.post(f"/api/users/{self.warehouse.pk}/reset-password")
        self.assertEqual(response.status_code, 200)
        self.warehouse.refresh_from_db()
        self.assertTrue(self.warehouse.must_change_password)
        self.assertTrue(self.warehouse.check_password(response.data["temp_password"]))
        response = self.client.post(f"/api/users/{self.warehouse.pk}/unlink-telegram")
        self.assertEqual(response.data["detail"], "Telegram uzildi")
        self.warehouse.refresh_from_db()
        self.assertIsNone(self.warehouse.telegram_id)

    def test_list_filters(self):
        self.login_as(self.admin)
        response = self.client.get("/api/users?role=OWNER")
        self.assertEqual({u["username"] for u in response.data["results"]}, {"owner_a", "owner_b"})
        response = self.client.get("/api/users?search=ombor")
        self.assertEqual([u["username"] for u in response.data["results"]], ["ombor1"])
        self.assertEqual(self.client.get("/api/users?role=BAD").status_code, 400)


@override_settings(BOT_TOKEN=BOT_TOKEN)
class TelegramAuthTests(BaseAPITestCase):
    url = "/api/auth/telegram"

    def test_linked_user_gets_tokens(self):
        User.objects.filter(pk=self.owner_a.pk).update(telegram_id=4242)
        response = self.client.post(self.url, {"init_data": signed_init_data(4242)}, format="json")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["user"]["username"], "owner_a")
        self.assertIn("access", response.data)

    def test_tampered_payload_is_401(self):
        User.objects.filter(pk=self.owner_a.pk).update(telegram_id=4242)
        original = signed_init_data(4243)
        tampered = original.replace("%3A4243%2C", "%3A4242%2C")
        self.assertNotEqual(original, tampered)
        response = self.client.post(self.url, {"init_data": tampered}, format="json")
        self.assertEqual(response.status_code, 401)

    def test_not_linked_is_403(self):
        response = self.client.post(self.url, {"init_data": signed_init_data(999)}, format="json")
        self.assertEqual(response.status_code, 403)
        self.assertEqual(
            response.data["detail"], "Telegram akkauntingiz tizimga bog‘lanmagan. Botda /start bosing."
        )

    def test_blocked_is_403(self):
        User.objects.filter(pk=self.owner_a.pk).update(telegram_id=4242, is_active=False)
        response = self.client.post(self.url, {"init_data": signed_init_data(4242)}, format="json")
        self.assertEqual(response.status_code, 403)

    @override_settings(BOT_TOKEN="")
    def test_empty_bot_token_is_401(self):
        User.objects.filter(pk=self.owner_a.pk).update(telegram_id=4242)
        response = self.client.post(self.url, {"init_data": signed_init_data(4242)}, format="json")
        self.assertEqual(response.status_code, 401)

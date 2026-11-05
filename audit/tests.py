"""Audit journal (API_CONTRACT_V2 §2): one row per business change, same transaction, immutable, no secrets."""

from __future__ import annotations

import json
import time
from datetime import timedelta
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.db import transaction
from django.test import TestCase, override_settings
from django.utils import timezone

from accounts.models import User
from accounts.telegram import build_data_check_string, sign
from common.roles import Role
from common.testing import BOT_API_TOKEN, BOT_TOKEN, BaseAPITestCase, make_product, make_user

from . import services
from .context import audit_context, client_ip
from .models import AuditLog, AuditLogImmutable

FORBIDDEN_FRAGMENTS = ("password", "token", "secret")
FORBIDDEN_EXACT = {"access", "refresh", "init_data"}


def forbidden_keys(value, path="") -> list[str]:
    """Every key (recursively) that must never be stored."""
    found = []
    if isinstance(value, dict):
        for key, item in value.items():
            lowered = key.lower()
            if lowered != "must_change_password" and (
                lowered in FORBIDDEN_EXACT or any(fragment in lowered for fragment in FORBIDDEN_FRAGMENTS)
            ):
                found.append(f"{path}{key}")
            found.extend(forbidden_keys(item, f"{path}{key}."))
    elif isinstance(value, list):
        for item in value:
            found.extend(forbidden_keys(item, path))
    return found


def telegram_init_data(telegram_id: int, bot_token: str = BOT_TOKEN) -> str:
    from urllib.parse import urlencode

    fields = {
        "user": json.dumps({"id": telegram_id, "first_name": "Ali"}, separators=(",", ":")),
        "auth_date": str(int(time.time())),
    }
    fields["hash"] = sign(build_data_check_string(fields), bot_token)
    return urlencode(fields)


class AuditTestCase(BaseAPITestCase):
    def rows(self, **filters):
        return list(AuditLog.objects.filter(**filters).order_by("id"))

    def only_row(self, **filters) -> AuditLog:
        rows = self.rows(**filters)
        self.assertEqual(len(rows), 1, [(r.module, r.action, r.object_repr) for r in AuditLog.objects.all()])
        return rows[0]

    def assert_no_secrets(self):
        for row in AuditLog.objects.all():
            self.assertEqual(forbidden_keys(row.before or {}) + forbidden_keys(row.after or {}), [], row)


class ServiceLevelTests(TestCase):
    def test_no_context_means_no_row(self):
        user = make_user("u1", Role.ADMIN)
        self.assertIsNone(services.record(module="users", action="update", obj=user, actor=user))
        self.assertEqual(AuditLog.objects.count(), 0)

    def test_sanitize_is_recursive(self):
        data = {
            "password": "x", "new_password": "y", "temp_password": "z", "access": "a", "refresh": "r",
            "init_data": "i", "token_version": 3, "MyToken": "t", "client_secret": "s",
            "must_change_password": True, "ok": {"nested": [{"Password_hash": 1, "keep": Decimal("1.50")}]},
        }
        self.assertEqual(
            services.sanitize(data), {"must_change_password": True, "ok": {"nested": [{"keep": 1.5}]}}
        )

    def test_json_values(self):
        now = timezone.now()
        self.assertEqual(services.to_json(Decimal("100.00")), 100)
        self.assertEqual(services.to_json(Decimal("39.654120")), 39.65412)
        self.assertEqual(services.to_json(now.date()), now.date().isoformat())
        self.assertTrue(services.to_json(now).endswith("+05:00"))

    def test_diff_only_changed_keys(self):
        self.assertEqual(services.diff({"a": 1, "b": 2}, {"a": 1, "b": 3}), ({"b": 2}, {"b": 3}))
        self.assertEqual(services.diff({"a": 1}, {"a": 1}), ({}, {}))

    def test_client_ip(self):
        with override_settings(REST_FRAMEWORK={"NUM_PROXIES": 1}):
            from rest_framework.settings import api_settings

            api_settings.reload()
            try:
                self.assertEqual(client_ip({"REMOTE_ADDR": "10.0.0.1"}), "10.0.0.1")
                self.assertEqual(
                    client_ip({"REMOTE_ADDR": "10.0.0.1", "HTTP_X_FORWARDED_FOR": "1.1.1.1, 203.0.113.5"}),
                    "203.0.113.5",
                )
                self.assertIsNone(client_ip({"REMOTE_ADDR": "", "HTTP_X_FORWARDED_FOR": "not-an-ip"}))
            finally:
                api_settings.reload()


class ImmutabilityTests(TestCase):
    def setUp(self):
        with audit_context("API", "127.0.0.1"):
            self.row = services.record(module="users", action="create", actor=None, after={"username": "x"})

    def test_model_and_queryset_writes_raise(self):
        self.row.object_repr = "changed"
        with self.assertRaises(AuditLogImmutable):
            self.row.save()
        with self.assertRaises(AuditLogImmutable):
            self.row.delete()
        with self.assertRaises(AuditLogImmutable):
            AuditLog.objects.filter(pk=self.row.pk).update(object_repr="x")
        with self.assertRaises(AuditLogImmutable):
            AuditLog.objects.all().delete()
        self.assertEqual(AuditLog.objects.get(pk=self.row.pk).object_repr, "")

    def test_deleting_the_actor_keeps_the_row(self):
        user = make_user("gone", Role.ADMIN)
        with audit_context("API"):
            row = services.record(module="users", action="update", obj=user, actor=user, after={"a": 1})
        user.delete()
        row = AuditLog.objects.get(pk=row.pk)
        self.assertIsNone(row.actor_id)
        self.assertEqual(row.actor_name, "Gone (gone)")


class AuthAuditTests(AuditTestCase):
    def login(self, username: str, password: str, **extra):
        return self.client.post("/api/auth/login", {"username": username, "password": password}, format="json",
                                **extra)

    def test_login_success_and_failures(self):
        self.assertEqual(self.login("admin1", "Parol12345", HTTP_X_FORWARDED_FOR="203.0.113.5").status_code, 200)
        row = self.only_row(action="login_success")
        self.assertEqual(
            (row.module, row.source, row.actor_id, row.role, row.object_type, row.object_id, row.ip, row.after),
            ("auth", "API", self.admin.pk, "ADMIN", "User", self.admin.pk, "203.0.113.5", {"method": "password"}),
        )
        self.assertEqual(row.actor_name, "Admin1 (admin1)")

        self.assertEqual(self.login("admin1", "wrong-pass").status_code, 401)
        self.assertEqual(self.login("nobody_here", "wrong-pass").status_code, 401)
        User.objects.filter(pk=self.warehouse.pk).update(is_active=False)
        self.assertEqual(self.login("ombor1", "Parol12345").status_code, 403)
        failures = self.rows(action="login_failure")
        self.assertEqual(
            [(r.actor_id, r.actor_name, r.after["reason"]) for r in failures],
            [(self.admin.pk, "Admin1 (admin1)", "bad_credentials"),
             (None, "no***", "bad_credentials"),
             (self.warehouse.pk, "Ombor1 (ombor1)", "blocked")],
        )
        self.assertEqual(failures[1].role, "")
        self.assertIsNone(failures[1].object_id)
        self.assert_no_secrets()

    def test_password_typed_as_username_is_not_stored(self):
        """A password typed into the login field must never land in the immutable audit table."""
        self.assertEqual(self.login("MySecret-Pass#2026", "x").status_code, 401)
        self.assertEqual(self.login("Qwerty12345", "x").status_code, 401)
        self.assertEqual(self.login("a" * 151, "x").status_code in (400, 401), True)
        names = [row.actor_name for row in self.rows(action="login_failure")]
        self.assertEqual(names[:2], ["(noto‘g‘ri login)", "Qw***"])
        for name in names:
            self.assertNotIn("Secret", name)
            self.assertNotIn("12345", name)

    def test_validation_error_and_throttle_write_nothing(self):
        self.assertEqual(self.client.post("/api/auth/login", {"username": ""}, format="json").status_code, 400)
        self.assertEqual(AuditLog.objects.count(), 0)
        # The rejected request above already used 1 of the 10 per-minute attempts of this IP.
        for _ in range(9):
            self.assertEqual(self.login("nobody", "x").status_code, 401)
        self.assertEqual(AuditLog.objects.count(), 9)
        self.assertEqual(self.login("nobody", "x").status_code, 429)
        self.assertEqual(AuditLog.objects.count(), 9)

    @override_settings(BOT_TOKEN=BOT_TOKEN)
    def test_telegram_login(self):
        User.objects.filter(pk=self.owner_a.pk).update(telegram_id=777)
        ok = self.client.post("/api/auth/telegram", {"init_data": telegram_init_data(777)}, format="json")
        self.assertEqual(ok.status_code, 200)
        self.assertEqual(self.only_row(action="login_success").after, {"method": "telegram"})
        self.client.post("/api/auth/telegram", {"init_data": telegram_init_data(777, "1:other")}, format="json")
        self.client.post("/api/auth/telegram", {"init_data": telegram_init_data(778)}, format="json")
        User.objects.filter(pk=self.owner_a.pk).update(is_active=False)
        self.client.post("/api/auth/telegram", {"init_data": telegram_init_data(777)}, format="json")
        self.assertEqual(
            [(r.after["reason"], r.actor_id) for r in self.rows(action="login_failure")],
            [("telegram_invalid", None), ("telegram_not_linked", None), ("blocked", self.owner_a.pk)],
        )
        self.assert_no_secrets()

    def test_change_password(self):
        self.login_as(self.owner_a)
        response = self.client.post(
            "/api/auth/change-password", {"old_password": "Parol12345", "new_password": "Yangi-Parol-2026"},
            format="json",
        )
        self.assertEqual(response.status_code, 200)
        row = self.only_row(action="password_change")
        self.assertEqual((row.module, row.actor_id, row.object_id, row.before, row.after),
                         ("auth", self.owner_a.pk, self.owner_a.pk, None, {"must_change_password": False}))
        self.assert_no_secrets()


class UserAuditTests(AuditTestCase):
    def setUp(self):
        super().setUp()
        self.login_as(self.admin)

    def test_create_update_reset_unlink(self):
        created = self.client.post(
            "/api/users", {"username": "yangi_ombor", "first_name": "Jasur", "role": "WAREHOUSE"}, format="json"
        ).json()
        uid = created["user"]["id"]
        row = self.only_row(module="users", action="create")
        self.assertEqual(row.after["username"], "yangi_ombor")
        self.assertEqual(row.after["must_change_password"], True)
        self.assertIsNone(row.before)
        self.assertEqual((row.actor_id, row.role, row.source), (self.admin.pk, "ADMIN", "API"))

        self.client.patch(f"/api/users/{uid}", {"phone": "+998901112233", "first_name": "Jasur"}, format="json")
        update = self.only_row(module="users", action="update")
        self.assertEqual((update.before, update.after), ({"phone": ""}, {"phone": "+998901112233"}))
        self.client.patch(f"/api/users/{uid}", {"phone": "+998901112233"}, format="json")  # no-op
        self.client.patch(f"/api/users/{uid}", {"is_active": False}, format="json")
        self.assertEqual([r.after for r in self.rows(module="users", action="update")],
                         [{"phone": "+998901112233"}, {"is_active": False}])

        self.client.post(f"/api/users/{uid}/reset-password")
        reset = self.only_row(action="password_reset")
        self.assertEqual(reset.after, {"must_change_password": True})

        User.objects.filter(pk=uid).update(telegram_id=4242)
        self.client.post(f"/api/users/{uid}/unlink-telegram")
        self.client.post(f"/api/users/{uid}/unlink-telegram")  # already unlinked → no row
        unlink = self.only_row(module="telegram", action="unlink")
        self.assertEqual((unlink.before, unlink.after), ({"telegram_id": 4242}, {"telegram_id": None}))
        self.assert_no_secrets()

    def test_validation_error_writes_nothing(self):
        self.client.post("/api/users", {"username": "x", "role": "OWNER"}, format="json")
        self.client.patch(f"/api/users/{self.admin.pk}", {"is_active": False}, format="json")  # cannot block self
        self.assertEqual(AuditLog.objects.count(), 0)


@override_settings(BOT_API_TOKEN=BOT_API_TOKEN)
class BotAuditTests(AuditTestCase):
    def bot(self, url, data):
        return self.client.post(url, data, format="json", HTTP_X_BOT_TOKEN=BOT_API_TOKEN,
                                HTTP_X_FORWARDED_FOR="203.0.113.9")

    def test_link_relink_unlink_location(self):
        self.assertEqual(self.bot("/api/bot/link", {"telegram_id": 900, "username": "owner_a",
                                                    "password": "Parol12345"}).status_code, 200)
        link = self.only_row(action="link")
        self.assertEqual((link.source, link.ip, link.actor_id, link.before, link.after),
                         ("BOT", None, self.owner_a.pk, {"telegram_id": None}, {"telegram_id": 900}))
        self.bot("/api/bot/link", {"telegram_id": 900, "username": "owner_a", "password": "Parol12345"})
        self.assertEqual(len(self.rows(action="link")), 1)  # same link again → no-op

        self.bot("/api/bot/link", {"telegram_id": 900, "username": "owner_b", "password": "Parol12345"})
        relinked = self.only_row(action="unlink")
        self.assertEqual((relinked.object_id, relinked.actor_id, relinked.after),
                         (self.owner_a.pk, self.owner_b.pk, {"telegram_id": None, "reason": "relinked"}))

        self.bot("/api/bot/location", {"telegram_id": 900, "latitude": 39.5, "longitude": 66.9})
        location = self.only_row(module="households", action="update")
        self.assertEqual((location.source, location.object_id, location.after),
                         ("BOT", self.household_b.pk, {"latitude": 39.5, "longitude": 66.9}))

        self.bot("/api/bot/unlink", {"telegram_id": 900})
        self.assertEqual(len(self.rows(action="unlink")), 2)
        self.assert_no_secrets()


class FarmAndStockAuditTests(AuditTestCase):
    def setUp(self):
        super().setUp()
        self.login_as(self.admin)

    def test_household_create_and_update(self):
        response = self.client.post(
            "/api/households",
            {"name": "Yangi uy", "owner_first_name": "A", "owner_last_name": "B", "owner_username": "yangi_egasi",
             "phone": "+998901234567", "region": "Samarqand viloyati", "district": "Navobod tumani"},
            format="json",
        )
        hid = response.json()["household"]["id"]
        self.assertEqual([(r.module, r.action) for r in self.rows()], [("users", "create"), ("households", "create")])
        household_row = self.only_row(module="households")
        self.assertEqual(household_row.after["code"], f"XN-{hid:04d}")
        self.assertEqual(household_row.object_repr, f"Yangi uy (XN-{hid:04d})")
        self.assertEqual(self.only_row(module="users").after["role"], "OWNER")

        self.client.patch(f"/api/households/{hid}", {"name": "Yangi uy"}, format="json")  # no-op
        self.client.patch(f"/api/households/{hid}", {"is_active": False, "name": "Eski uy"}, format="json")
        update = self.only_row(module="households", action="update")
        self.assertEqual(update.before, {"name": "Yangi uy", "is_active": True})
        self.assertEqual(update.after, {"name": "Eski uy", "is_active": False})

    def test_batch_movement_report_stock(self):
        response = self.client.post(
            "/api/batches",
            {"poultry_type": "BROILER", "breed": "Ross 308", "initial_quantity": 100, "age_days": 1,
             "supplier": "Inkubator", "arrived_at": (timezone.localdate() - timedelta(days=2)).isoformat()},
            format="json",
        )
        batch_id = response.json()["id"]
        batch_row = self.only_row(module="batches", action="create")
        self.assertEqual(batch_row.after["initial_quantity"], 100)
        self.client.patch(f"/api/batches/{batch_id}", {"health_status": "Kasallik belgilari"}, format="json")
        self.client.patch(f"/api/batches/{batch_id}", {"health_status": "Kasallik belgilari"}, format="json")
        update = self.only_row(module="batches", action="update")
        self.assertEqual((update.before, update.after),
                         ({"health_status": "Sog‘lom"}, {"health_status": "Kasallik belgilari"}))

        movement = self.client.post(
            "/api/movements", {"batch": batch_id, "household": self.household_a.pk, "type": "DISTRIBUTE",
                                "quantity": 40}, format="json",
        ).json()
        row = self.only_row(module="movements")
        self.assertEqual(row.op_id, movement["op_id"])
        self.assertEqual(row.after["batch_code"], movement["batch"]["code"])
        self.assertEqual(row.object_repr, f"{movement['op_id']} Xonadonga tarqatish 40")

        self.login_as(self.owner_a)
        report = self.client.post("/api/daily-reports", {"feed_kg": "12.50", "meat_kg": "6.25", "deaths": 2},
                                  format="json").json()
        movements = self.rows(module="movements")
        self.assertEqual(len(movements), 2)
        death = movements[1]
        self.assertEqual((death.after["type"], death.after["quantity"], death.actor_id),
                         ("DEATH", 2, self.owner_a.pk))
        report_row = self.only_row(module="daily_reports")
        self.assertEqual(report_row.op_id, f"KH-{report['id']:06d}")
        self.assertEqual(report_row.after["death_op_id"], death.op_id)
        self.assertEqual(report_row.after["feed_kg"], 12.5)
        self.assertEqual(report_row.role, "OWNER")

        self.login_as(self.warehouse)
        product = self.client.post("/api/products", {"name": "Yem X", "category": "FEED", "unit": "kg",
                                                     "min_stock": 10}, format="json").json()
        self.client.patch(f"/api/products/{product['id']}", {"min_stock": "10.00"}, format="json")  # no-op
        self.client.patch(f"/api/products/{product['id']}", {"min_stock": 25}, format="json")
        self.assertEqual(self.only_row(module="products", action="update").after, {"min_stock": 25})
        self.client.post("/api/stock-transactions", {"product": product["id"], "type": "IN", "quantity": "100.5",
                                                     "document_no": "K-1"}, format="json")
        self.client.post("/api/stock-transactions", {"product": product["id"], "type": "OUT", "quantity": "30.25",
                                                     "document_no": "C-1", "household": self.household_a.pk},
                         format="json")
        stock = self.rows(module="stock")
        self.assertEqual([(r.after["balance_before"], r.after["balance_after"]) for r in stock],
                         [(0, 100.5), (100.5, 70.25)])
        self.assertEqual(stock[1].after["product_name"], "Yem X")
        self.assertEqual(stock[1].after["household_id"], self.household_a.pk)
        self.assert_no_secrets()

    def test_rejected_operations_leave_no_row(self):
        self.client.post("/api/movements", {"batch": self.batch.pk, "household": self.household_a.pk,
                                            "type": "DISTRIBUTE", "quantity": 10**6}, format="json")
        self.client.patch(f"/api/batches/{self.batch.pk}", {"breed": "X"}, format="json")
        product = make_product()
        self.client.post("/api/stock-transactions", {"product": product.pk, "type": "OUT", "quantity": 5,
                                                     "document_no": "C-1"}, format="json")
        self.assertEqual(AuditLog.objects.count(), 0)

    def test_rollback_removes_the_audit_row(self):
        from farm import services as farm_services

        with audit_context("API", "127.0.0.1"):
            with self.assertRaises(RuntimeError), transaction.atomic():
                farm_services.update_household(self.household_a, {"name": "Rollback"}, actor=self.admin)
                self.assertEqual(AuditLog.objects.count(), 1)
                raise RuntimeError("business failure after the audit insert")
        self.assertEqual(AuditLog.objects.count(), 0)


class AuditLogApiTests(AuditTestCase):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        with audit_context("API", "203.0.113.5"):
            services.record(module="households", action="update", obj=cls.household_a, actor=cls.admin,
                            before={"name": "A"}, after={"name": "B"})
        with audit_context("BOT"):
            services.record(module="telegram", action="link", obj=cls.owner_a, actor=cls.owner_a,
                            before={"telegram_id": None}, after={"telegram_id": 1})

    def test_permissions_and_write_methods(self):
        for user, code in ((self.admin, 200), (self.warehouse, 403), (self.owner_a, 403), (None, 401)):
            self.client.force_authenticate(user=user)
            self.assertEqual(self.client.get("/api/audit-logs").status_code, code)
        self.login_as(self.admin)
        row_id = AuditLog.objects.first().pk
        self.assertEqual(self.client.post("/api/audit-logs", {}, format="json").status_code, 405)
        for method in ("put", "patch", "delete"):
            self.assertEqual(getattr(self.client, method)(f"/api/audit-logs/{row_id}", {}, format="json").status_code,
                             405)
        self.assertEqual(AuditLog.objects.count(), 2)

    def test_item_shape_and_filters(self):
        self.login_as(self.admin)
        data = self.client.get("/api/audit-logs").json()
        self.assertEqual(data["count"], 2)
        newest = data["results"][0]
        self.assertEqual(newest["module"], "telegram")
        self.assertEqual(newest["action_display"], "Telegram bog‘landi")
        oldest = data["results"][1]
        self.assertEqual(
            {key: oldest[key] for key in ("actor", "actor_name", "role", "source", "module", "module_display",
                                          "action", "action_display", "object_type", "object_id", "before",
                                          "after", "ip", "op_id")},
            {"actor": {"id": self.admin.pk, "full_name": "Admin1"}, "actor_name": "Admin1 (admin1)", "role": "ADMIN",
             "source": "API", "module": "households", "module_display": "Xonadonlar", "action": "update",
             "action_display": "O‘zgartirildi", "object_type": "Household", "object_id": self.household_a.pk,
             "before": {"name": "A"}, "after": {"name": "B"}, "ip": "203.0.113.5", "op_id": ""},
        )
        today = timezone.localdate().isoformat()

        def count(query: str) -> int:
            response = self.client.get(f"/api/audit-logs?{query}")
            self.assertEqual(response.status_code, 200, response.content)
            return response.json()["count"]

        self.assertEqual(count("module=telegram"), 1)
        self.assertEqual(count("action=update"), 1)
        self.assertEqual(count(f"actor={self.owner_a.pk}"), 1)
        self.assertEqual(count(f"object_type=Household&object_id={self.household_a.pk}"), 1)
        self.assertEqual(count("source=BOT"), 1)
        self.assertEqual(count(f"date_from={today}&date_to={today}"), 2)
        self.assertEqual(count("date_to=2000-01-01"), 0)
        self.assertEqual(count("search=admin1"), 1)
        self.assertEqual(count("search=203.0.113.5"), 1)
        self.assertEqual(count("search=NAVBAHOR"), 1)

    def test_filter_errors(self):
        self.login_as(self.admin)
        cases = {
            "module=nope": {"module": ["“nope” ruxsat etilgan qiymat emas."]},
            "source=WEB": {"source": ["“WEB” ruxsat etilgan qiymat emas."]},
            "object_type=Car": {"object_type": ["“Car” ruxsat etilgan qiymat emas."]},
            "actor=x": {"actor": ["Butun son kiriting."]},
            "object_id=1.5": {"object_id": ["Butun son kiriting."]},
            "date_from=2026-02-30": {"date_from": ["Sana noto‘g‘ri formatda (YYYY-MM-DD)."]},
            "date_from=2026-11-04&date_to=2026-11-01": {
                "date_from": ["Boshlanish sanasi tugash sanasidan keyin bo‘lishi mumkin emas."]},
        }
        for query, expected in cases.items():
            with self.subTest(query=query):
                response = self.client.get(f"/api/audit-logs?{query}")
                self.assertEqual((response.status_code, response.json()), (400, expected))
        page = self.client.get("/api/audit-logs?page=99")
        self.assertEqual((page.status_code, page.json()), (404, {"detail": "Sahifa topilmadi."}))
        self.assertEqual(self.client.get("/api/audit-logs?date_from=2001-01-01").status_code, 200)  # no 366 limit

    def test_query_budget(self):
        self.login_as(self.admin)
        with self.assertNumQueries(2):
            self.client.get("/api/audit-logs")


class DjangoAdminAuditTests(AuditTestCase):
    def setUp(self):
        super().setUp()
        self.superuser = get_user_model().objects.create_superuser("root", password="Parol12345")
        self.client.force_login(self.superuser)

    def test_admin_change_is_logged_and_audit_admin_is_read_only(self):
        household = self.household_a
        response = self.client.post(
            f"/admin/farm/household/{household.pk}/change/",
            {"name": "Admin orqali", "phone": household.phone, "region": household.region,
             "district": household.district, "mfy": household.mfy, "address": household.address,
             "latitude": "", "longitude": "", "is_active": "on", "contract_info": ""},
        )
        self.assertEqual(response.status_code, 302, response.content[:2000])
        row = self.only_row(module="households", action="update")
        self.assertEqual((row.source, row.actor_id, row.after), ("ADMIN", self.superuser.pk, {"name": "Admin orqali"}))

        self.assertEqual(self.client.get("/admin/audit/auditlog/").status_code, 200)
        self.assertEqual(self.client.get("/admin/audit/auditlog/add/").status_code, 403)
        self.assertEqual(self.client.post(f"/admin/audit/auditlog/{row.pk}/delete/", {"post": "yes"}).status_code, 403)
        self.assertTrue(AuditLog.objects.filter(pk=row.pk).exists())

    def test_admin_password_change_revokes_sessions(self):
        version = self.owner_a.token_version
        response = self.client.post(
            f"/admin/accounts/user/{self.owner_a.pk}/password/",
            {"password1": "Yangi-Parol-2026", "password2": "Yangi-Parol-2026", "usable_password": "true"},
        )
        self.assertEqual(response.status_code, 302)
        self.owner_a.refresh_from_db()
        self.assertEqual(self.owner_a.token_version, version + 1)
        row = self.only_row(action="password_reset")
        self.assertEqual((row.source, row.actor_id), ("ADMIN", self.superuser.pk))
        self.assert_no_secrets()


class DjangoAdminUserAuditTests(AuditTestCase):
    """Users are never deleted in Django admin; group / permission grants are audited."""

    def setUp(self):
        super().setUp()
        self.superuser = get_user_model().objects.create_superuser("root", password="Parol12345")
        self.client.force_login(self.superuser)

    def test_user_delete_is_forbidden(self):
        self.assertEqual(self.client.get(f"/admin/accounts/user/{self.warehouse.pk}/delete/").status_code, 403)
        response = self.client.post(f"/admin/accounts/user/{self.warehouse.pk}/delete/", {"post": "yes"})
        self.assertEqual(response.status_code, 403)
        response = self.client.post(
            "/admin/accounts/user/", {"action": "delete_selected", "_selected_action": [self.warehouse.pk]}
        )
        self.assertTrue(User.objects.filter(pk=self.warehouse.pk).exists())

    def test_permission_grant_is_audited(self):
        from django.contrib.auth.models import Group, Permission

        group = Group.objects.create(name="Ko‘ruvchilar")
        permission = Permission.objects.get(content_type__app_label="farm", codename="view_household")
        user = self.warehouse
        response = self.client.post(
            f"/admin/accounts/user/{user.pk}/change/",
            {
                "username": user.username, "first_name": user.first_name, "last_name": user.last_name,
                "email": "", "is_active": "on", "is_staff": "on", "groups": [group.pk],
                "user_permissions": [permission.pk], "last_login_0": "", "last_login_1": "",
                "date_joined_0": "2026-01-01", "date_joined_1": "10:00:00", "role": user.role,
                "phone": user.phone, "telegram_id": "", "must_change_password": "",
            },
        )
        self.assertEqual(response.status_code, 302, response.content[:3000])
        rows = self.rows(module="users", action="update", object_id=user.pk)
        access = [row for row in rows if "groups" in (row.after or {})]
        self.assertEqual(len(access), 1)
        self.assertEqual(access[0].source, "ADMIN")
        self.assertEqual(access[0].before, {"groups": [], "user_permissions": []})
        self.assertEqual(access[0].after, {"groups": ["Ko‘ruvchilar"], "user_permissions": ["farm.view_household"]})
        self.assert_no_secrets()


class AuditRefsAndStatusTests(AuditTestCase):
    """TZ §14: which household / product a row belongs to, and the operation status."""

    def test_household_and_product_refs_and_filters(self):
        from common.testing import make_product

        self.login_as(self.admin)
        product = make_product("Yem PK-9", min_stock="0")
        response = self.client.post("/api/stock-transactions", {"product": product.pk, "type": "IN", "quantity": "50",
                                                                 "document_no": "K-9"}, format="json")
        self.assertEqual(response.status_code, 201, response.data)
        response = self.client.post("/api/stock-transactions", {"product": product.pk, "type": "OUT", "quantity": "5",
                                                                 "document_no": "C-9", "household": self.household_a.pk},
                                    format="json")
        self.assertEqual(response.status_code, 201, response.data)
        self.client.patch(f"/api/households/{self.household_a.pk}", {"name": "Navbahor 12-uy (yangi)"}, format="json")

        out_row = self.rows(module="stock")[-1]
        self.assertEqual((out_row.household_ref, out_row.product_ref, out_row.status),
                         (self.household_a.pk, product.pk, "SUCCESS"))
        rows = self.client.get(f"/api/audit-logs?household={self.household_a.pk}").data["results"]
        self.assertEqual({row["module"] for row in rows}, {"stock", "households"})
        self.assertTrue(all(row["household_ref"] == self.household_a.pk for row in rows))
        rows = self.client.get(f"/api/audit-logs?product={product.pk}").data["results"]
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["status_display"], "Muvaffaqiyatli")

    def test_failed_login_has_failed_status(self):
        from rest_framework.test import APIClient

        APIClient().post("/api/auth/login", {"username": "admin1", "password": "noto‘g‘ri"}, format="json")
        row = self.only_row(action="login_failure")
        self.assertEqual(row.status, "FAILED")
        self.login_as(self.admin)
        rows = self.client.get("/api/audit-logs?status=FAILED").data["results"]
        self.assertEqual([row["action"] for row in rows], ["login_failure"])

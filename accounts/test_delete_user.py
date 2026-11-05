"""Deleting staff accounts: DELETE /api/users/{id} (kept as history, hidden, blocked, login freed)."""

from __future__ import annotations

from audit.models import AuditAction, AuditLog
from common.roles import Role
from common.testing import BaseAPITestCase, make_user
from warehouse.models import Product

from .models import User


class DeleteUserTests(BaseAPITestCase):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.vet = make_user("vet_del", Role.VET, telegram_id=555000111)
        cls.root = make_user("root_del", Role.ADMIN, is_superuser=True, is_staff=True)

    def test_admin_deletes_staff_account(self):
        self.login_as(self.admin)
        response = self.client.delete(f"/api/users/{self.vet.pk}")
        self.assertEqual(response.status_code, 204)
        self.vet.refresh_from_db()
        self.assertIsNotNone(self.vet.deleted_at)
        self.assertFalse(self.vet.is_active)
        self.assertIsNone(self.vet.telegram_id)
        self.assertTrue(self.vet.username.startswith(f"deleted-{self.vet.pk}-"))
        # Hidden everywhere in the user API
        self.assertEqual(self.client.get(f"/api/users/{self.vet.pk}").status_code, 404)
        ids = [row["id"] for row in self.client.get("/api/users?page_size=200").data["results"]]
        self.assertNotIn(self.vet.pk, ids)
        # Audited
        self.assertTrue(AuditLog.objects.filter(action=AuditAction.DELETE, object_id=self.vet.pk).exists())

    def test_deleted_user_cannot_log_in_and_login_name_is_free(self):
        self.login_as(self.admin)
        self.client.delete(f"/api/users/{self.vet.pk}")
        self.client.force_authenticate(user=None)
        response = self.client.post("/api/auth/login", {"username": "vet_del", "password": "Parol12345"}, format="json")
        self.assertEqual(response.status_code, 401)
        self.login_as(self.admin)
        response = self.client.post(
            "/api/users",
            {"username": "vet_del", "first_name": "Yangi", "phone": "+998901112233", "role": "VET"},
            format="json",
        )
        self.assertEqual(response.status_code, 201)

    def test_history_rows_survive(self):
        product = Product.objects.create(name="Yem test", category="FEED", unit="kg", min_stock=0)
        self.login_as(self.warehouse)
        self.client.post(
            "/api/stock-transactions",
            {"product": product.pk, "type": "IN", "quantity": 10, "document_no": "K-1"},
            format="json",
        )
        self.login_as(self.admin)
        self.assertEqual(self.client.delete(f"/api/users/{self.warehouse.pk}").status_code, 204)
        self.assertTrue(User.objects.filter(pk=self.warehouse.pk).exists())
        product.refresh_from_db()
        self.assertEqual(product.balance, 10)

    def test_cannot_delete_self(self):
        self.login_as(self.admin)
        response = self.client.delete(f"/api/users/{self.admin.pk}")
        self.assertEqual(response.status_code, 400)
        self.admin.refresh_from_db()
        self.assertIsNone(self.admin.deleted_at)

    def test_cannot_delete_household_owner(self):
        self.login_as(self.admin)
        response = self.client.delete(f"/api/users/{self.owner_a.pk}")
        self.assertEqual(response.status_code, 400)
        self.owner_a.refresh_from_db()
        self.assertIsNone(self.owner_a.deleted_at)

    def test_admin_cannot_see_or_delete_superuser(self):
        self.login_as(self.admin)
        self.assertEqual(self.client.delete(f"/api/users/{self.root.pk}").status_code, 404)

    def test_last_superuser_is_protected(self):
        other_root = make_user("root_two", Role.ADMIN, is_superuser=True, is_staff=True)
        self.login_as(other_root)
        self.assertEqual(self.client.delete(f"/api/users/{self.root.pk}").status_code, 204)
        # other_root is now the only active superuser; it cannot delete itself anyway, and a
        # deleted superuser no longer counts, so no account can remove the last one.
        self.assertEqual(self.client.delete(f"/api/users/{other_root.pk}").status_code, 400)

    def test_non_admin_roles_cannot_delete(self):
        for user in (self.warehouse, self.vet, self.owner_a):
            self.login_as(user)
            self.assertIn(self.client.delete(f"/api/users/{self.admin.pk}").status_code, (403, 404))

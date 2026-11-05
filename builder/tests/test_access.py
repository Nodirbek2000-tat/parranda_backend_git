"""Role profiles, middleware enforcement, /api/auth/me ``access`` and territorial scope (API_CONTRACT_V4 §2)."""

from __future__ import annotations

from accounts.services import issue_tokens
from audit.models import AuditLog
from builder.middleware import action_of, module_of
from builder.models import UserAccess
from common.roles import Role
from common.testing import make_household
from farm.models import Household

from .helpers import BuilderTestCase


class MiddlewareMappingTests(BuilderTestCase):
    def test_module_and_action_mapping(self):
        self.assertEqual(module_of("/api/stock-transactions"), "warehouse")
        self.assertEqual(module_of("/api/inventory-counts/3"), "warehouse")
        self.assertEqual(module_of("/api/process-runs"), "processes")
        self.assertEqual(module_of("/api/my/vet"), "households")
        self.assertIsNone(module_of("/api/settings"))
        self.assertEqual(action_of("GET", "/api/reports/stock/export", {}), "download")
        self.assertEqual(action_of("GET", "/api/acts/3/pdf", {}), "download")
        self.assertEqual(action_of("GET", "/api/stats", {"format": "xlsx"}), "download")
        self.assertEqual(action_of("GET", "/api/orders", {}), "view")
        self.assertEqual(action_of("POST", "/api/orders/3/approve", {}), "approve")
        self.assertEqual(action_of("POST", "/api/orders", {}), "create")
        self.assertEqual(action_of("PATCH", "/api/orders/3", {}), "edit")


class ProfileTests(BuilderTestCase):
    def jwt(self, user):
        user.refresh_from_db()
        token = issue_tokens(user)["access"]
        self.client.force_authenticate(user=None)
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {token}")

    def assign(self, user, profile=None, districts=(), households=()):
        self.as_user(self.superuser)
        return self.client.put(
            f"/api/builder/user-access/{user.pk}",
            {"profile_id": profile.pk if profile else None, "districts": list(districts),
             "household_ids": [h.pk for h in households]},
            format="json",
        )

    def test_assign_profile_sets_role_and_revokes_sessions(self):
        profile = self.role_profile({"warehouse": ["view"]}, base_role=Role.WAREHOUSE)
        before = self.vet.token_version
        response = self.assign(self.vet, profile)
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual((response.data["profile_id"], response.data["role"]), (profile.pk, "WAREHOUSE"))
        self.vet.refresh_from_db()
        self.assertEqual(self.vet.role, Role.WAREHOUSE)
        self.assertNotEqual(self.vet.token_version, before)
        self.assertTrue(AuditLog.objects.filter(module="builder", object_type="UserAccess").exists())
        self.assertTrue(AuditLog.objects.filter(module="users", action="sessions_revoked",
                                                object_id=self.vet.pk).exists())
        listed = self.client.get("/api/builder/user-access", {"profile": profile.pk}).data
        self.assertEqual([row["user_id"] for row in listed["results"]], [self.vet.pk])
        all_rows = self.client.get("/api/builder/user-access").data["results"]
        self.assertNotIn(self.owner_a.pk, [row["user_id"] for row in all_rows])

    def test_assign_validation(self):
        draft = self.role_profile({}, name="Qoralama")
        draft.status = "draft"
        draft.save()
        self.assertEqual(self.assign(self.vet, draft).data["profile_id"], ["Faqat faol rol profilini tanlash mumkin."])
        active = self.role_profile({})
        self.assertEqual(self.assign(self.superuser, active).status_code, 400)
        self.assertEqual(self.assign(self.owner_a, active).status_code, 404)
        self.as_user(self.admin)
        self.assertEqual(self.client.get("/api/builder/user-access").status_code, 403)

    def test_middleware_blocks_and_allows(self):
        profile = self.role_profile({"warehouse": ["view"], "orders": ["view", "approve"]})
        self.assign(self.warehouse, profile)
        self.jwt(self.warehouse)
        self.assertEqual(self.client.get("/api/products").status_code, 200)
        response = self.client.post("/api/products", {"name": "X"}, format="json")
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()["detail"], "Rolingizda bu amal uchun ruxsat yo‘q.")
        self.assertEqual(self.client.get("/api/stock-transactions").status_code, 200)
        self.assertEqual(self.client.get("/api/batches").status_code, 403)  # module missing
        self.assertEqual(self.client.get("/api/stats/overview").status_code, 403)
        me = self.client.get("/api/auth/me")
        self.assertEqual(me.status_code, 200)  # excluded path
        self.assertEqual(me.json()["access"]["profile_id"], profile.pk)
        self.assertEqual(me.json()["access"]["permissions"]["warehouse"], ["view"])
        self.assertEqual(self.client.get("/api/my/notifications").status_code, 200)
        # without a token the middleware lets DRF answer (401)
        self.client.credentials()
        self.assertEqual(self.client.get("/api/batches").status_code, 401)

    def test_user_without_profile_and_superuser_unrestricted(self):
        self.jwt(self.warehouse)
        self.assertEqual(self.client.get("/api/batches").status_code, 200)
        self.assertIsNone(self.client.get("/api/auth/me").json()["access"]["permissions"])
        self.jwt(self.superuser)
        self.assertEqual(self.client.get("/api/batches").status_code, 200)

    def test_scope_limits_households_and_tasks(self):
        other = make_household("Chust 1-uy", "owner_c")
        Household.objects.filter(pk=other.pk).update(district="Chust tumani")
        self.assign(self.vet, None, districts=["Chust tumani"])
        self.assertTrue(UserAccess.objects.filter(user=self.vet, districts=["Chust tumani"]).exists())
        self.as_user(self.vet)
        ids = [row["id"] for row in self.client.get("/api/households").data["results"]]
        self.assertEqual(ids, [other.pk])
        self.assertEqual(self.client.get(f"/api/households/{self.household_a.pk}").status_code, 404)
        template = self.task_template(approver="")
        self.as_user(self.admin)
        in_scope = self.client.post("/api/tasks", {"template_id": template.pk, "household_id": other.pk},
                                    format="json").data
        out_scope = self.client.post("/api/tasks", {"template_id": template.pk,
                                                    "household_id": self.household_a.pk}, format="json").data
        self.as_user(self.vet)
        ids = [row["id"] for row in self.client.get("/api/tasks").data["results"]]
        self.assertEqual(ids, [in_scope["id"]])
        self.assertEqual(self.client.get(f"/api/tasks/{out_scope['id']}").status_code, 404)
        # a single household grant widens the district scope
        self.assign(self.vet, None, districts=["Chust tumani"], households=[self.household_a])
        self.as_user(self.vet)
        self.assertEqual(self.client.get("/api/households").data["count"], 2)
        self.assertEqual(self.client.get("/api/auth/me").data["access"]["household_ids"], [self.household_a.pk])

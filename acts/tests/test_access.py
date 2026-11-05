"""Act access rules (API_CONTRACT_V4 §6 "Access") and scope (§2)."""

from __future__ import annotations

from builder.models import TaskStatus, UserAccess
from common.roles import Role
from common.testing import make_user

from .helpers import ActsTestCase


class ActAccessTests(ActsTestCase):
    def test_staff_only_own_role_and_forced_user(self):
        self.assertEqual(self.preview(self.vet, kind="role", role="WAREHOUSE").status_code, 403)
        self.assertEqual(self.preview(self.vet, kind="general").status_code, 403)
        self.assertEqual(self.preview(self.vet, kind="role", role="VET", user_id=self.vet2.pk).status_code, 403)
        response = self.create(self.vet, kind="role", role="VET")
        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(response.data["user"], {"id": self.vet.pk, "full_name": self.vet.full_name})
        self.assertEqual(self.preview(self.warehouse, kind="role", role="VET").status_code, 403)
        self.assertEqual(self.preview(self.logist, kind="role", role="ADMIN").status_code, 403)

    def test_owner_only_own_household(self):
        self.assertEqual(self.preview(self.owner_a, kind="role", role="VET").status_code, 403)
        self.assertEqual(self.preview(self.owner_a, kind="general", household_id=self.household_b.pk).status_code, 404)
        response = self.create(self.owner_a, kind="general")
        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(response.data["household"]["id"], self.household_a.pk)
        act_id = response.data["id"]
        self.login_as(self.owner_b)
        self.assertEqual(self.client.get(f"/api/acts/{act_id}").status_code, 404)
        self.assertEqual(self.client.get(f"/api/acts/{act_id}/pdf").status_code, 404)
        self.assertEqual(self.client.get("/api/acts").data["count"], 0)
        self.login_as(self.owner_a)
        self.assertEqual(self.client.get("/api/acts").data["count"], 1)

    def test_admin_user_must_match_role(self):
        response = self.preview(self.admin, kind="role", role="VET", user_id=self.logist.pk)
        self.assertEqual(response.status_code, 400)
        self.assertIn("user_id", response.data)
        snap = self.preview(self.admin, kind="role", role="OWNER", user_id=self.owner_a.pk).data
        self.assertEqual(snap["household"]["code"], self.household_a.code)

    def test_list_visibility_and_filters(self):
        self.create(self.vet, kind="role", role="VET")
        self.create(self.vet2, kind="role", role="VET")
        self.create(self.admin, kind="general")
        self.create(self.admin, kind="role", role="LOGISTICS", household_id=self.household_b.pk)
        self.login_as(self.vet)
        rows = self.client.get("/api/acts").data["results"]
        self.assertEqual([(r["kind"], r["user"]["id"]) for r in rows], [("role", self.vet.pk)])
        self.login_as(self.admin)
        self.assertEqual(self.client.get("/api/acts").data["count"], 4)
        self.assertEqual(self.client.get("/api/acts", {"kind": "general"}).data["count"], 1)
        self.assertEqual(self.client.get("/api/acts", {"role": "VET"}).data["count"], 2)
        self.assertEqual(self.client.get("/api/acts", {"household": self.household_b.pk}).data["count"], 1)
        self.assertEqual(self.client.get("/api/acts", {"kind": "nope"}).status_code, 400)

    def test_scope_limits_admin(self):
        scoped = make_user("admin_scoped", Role.ADMIN)
        access = UserAccess.objects.create(user=scoped, districts=["Pop tumani"])
        self.assertEqual(access.districts, ["Pop tumani"])
        self.assertEqual(self.preview(scoped, kind="general", household_id=self.household_a.pk).status_code, 404)
        snap = self.preview(scoped, kind="role", role="VET").data
        self.assertEqual(self.summary(snap)["O‘tkazilgan tekshiruvlar"], "0")  # visits are in Navobod
        self.assertEqual(self.preview(scoped, kind="general", household_id=self.household_c.pk).status_code, 200)
        self.create(self.admin, kind="general", household_id=self.household_a.pk)
        self.login_as(scoped)
        self.assertEqual(self.client.get("/api/acts").data["count"], 0)

    def test_task_act_access(self):
        # Approved task: executor role (VET), admin and the household owner may make it; others may not.
        for user in (self.vet2, self.admin, self.owner_a):
            self.assertEqual(self.preview(user, kind="task", task_id=self.task.pk).status_code, 200, user)
        for user in (self.owner_b, self.logist, self.warehouse):
            self.assertEqual(self.preview(user, kind="task", task_id=self.task.pk).status_code, 404, user)
        self.assertEqual(self.preview(self.admin, kind="task", task_id=self.open_task.pk).status_code, 409)
        self.assertEqual(self.preview(self.admin, kind="task").status_code, 400)
        test_task = self.make_task(self.household_a, status=TaskStatus.APPROVED, values={}, is_test=True)
        self.assertEqual(self.preview(self.admin, kind="task", task_id=test_task.pk).status_code, 404)
        superuser = make_user("root", Role.ADMIN, is_superuser=True)
        self.assertEqual(self.preview(superuser, kind="task", task_id=test_task.pk).status_code, 200)

    def test_anonymous(self):
        self.login_as(None)
        self.assertEqual(self.client.get("/api/acts").status_code, 401)

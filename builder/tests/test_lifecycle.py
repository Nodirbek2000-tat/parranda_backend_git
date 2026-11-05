"""Blueprint lifecycle API (API_CONTRACT_V4 §1.1): create, edit/pending, activate, revert, archive, delete."""

from __future__ import annotations

from audit.models import AuditLog, AuditModule
from builder.models import Blueprint, BlueprintKind, BlueprintStatus, ConfigVersion, UserAccess
from common.roles import Role

from .helpers import API, BuilderTestCase, number_form


class BlueprintApiTests(BuilderTestCase):
    def create(self, kind=BlueprintKind.FORM, data=None, name="Qurilish ko‘rigi"):
        self.as_user(self.superuser)
        return self.client.post(API, {"kind": kind, "name": name, "data": data or number_form()}, format="json")

    def test_superuser_only_writes_admin_reads_active(self):
        self.as_user(self.admin)
        response = self.client.post(API, {"kind": "form", "name": "X", "data": number_form()}, format="json")
        self.assertEqual(response.status_code, 403)
        created = self.create()
        self.assertEqual(created.status_code, 201, created.data)
        self.assertEqual((created.data["status"], created.data["version"]), ("draft", 1))
        self.assertEqual(created.data["usage"]["submissions"], 0)
        self.as_user(self.admin)
        self.assertEqual(self.client.get(f"{API}/{created.data['id']}").status_code, 404)  # draft hidden
        self.assertEqual(self.client.get("/api/builder/meta").status_code, 200)
        self.as_user(self.vet)
        self.assertEqual(self.client.get("/api/builder/meta").status_code, 403)
        self.assertTrue(AuditLog.objects.filter(module=AuditModule.BUILDER, action="create",
                                                object_type="Blueprint").exists())

    def test_validation_error_shape(self):
        response = self.create(data={"fields": [{"key": "Bad", "label": "x", "type": "text"}]})
        self.assertEqual(response.status_code, 400)
        self.assertIn("kalit", response.data["data"][0])

    def test_draft_edit_in_place_and_versions(self):
        pk = self.create().data["id"]
        response = self.client.patch(f"{API}/{pk}", {"name": "Yangi nom", "comment": "nom"}, format="json")
        self.assertEqual(response.status_code, 200)
        self.assertEqual((response.data["name"], response.data["version"], response.data["pending"]),
                         ("Yangi nom", 2, None))
        versions = self.client.get(f"{API}/{pk}/versions").data
        self.assertEqual([v["version"] for v in versions], [2, 1])
        self.assertEqual(versions[0]["comment"], "nom")

    def test_active_edit_goes_to_pending_until_activate(self):
        pk = self.create().data["id"]
        self.assertEqual(self.client.post(f"{API}/{pk}/activate").status_code, 200)
        self.assertEqual(self.client.post(f"{API}/{pk}/activate").data["detail"], "O‘zgarish yo‘q")
        response = self.client.patch(f"{API}/{pk}", {"name": "Tahrir"}, format="json")
        self.assertEqual((response.data["name"], response.data["version"]), ("Qurilish ko‘rigi", 1))
        self.assertEqual(response.data["pending"]["name"], "Tahrir")
        self.assertEqual(response.data["pending_by_name"], self.superuser.full_name)
        listed = self.client.get(API).data["results"][0]
        self.assertTrue(listed["has_pending"])
        self.assertNotIn("data", listed)
        discarded = self.client.post(f"{API}/{pk}/discard-pending").data
        self.assertIsNone(discarded["pending"])
        self.client.patch(f"{API}/{pk}", {"name": "Tahrir 2"}, format="json")
        activated = self.client.post(f"{API}/{pk}/activate", {"comment": "ok"}, format="json").data
        self.assertEqual((activated["name"], activated["version"], activated["pending"]), ("Tahrir 2", 2, None))

    def test_revert_and_duplicate(self):
        pk = self.create().data["id"]
        self.client.patch(f"{API}/{pk}", {"name": "v2"}, format="json")
        reverted = self.client.post(f"{API}/{pk}/revert", {"version": 1}, format="json")
        self.assertEqual((reverted.data["name"], reverted.data["version"]), ("Qurilish ko‘rigi", 3))
        self.assertEqual(self.client.post(f"{API}/{pk}/revert", {"version": 99}, format="json").status_code, 400)
        copy = self.client.post(f"{API}/{pk}/duplicate")
        self.assertEqual(copy.status_code, 201)
        self.assertEqual((copy.data["name"], copy.data["status"]), ("Qurilish ko‘rigi (nusxa)", "draft"))
        # revert on an active blueprint is parked as pending
        self.client.post(f"{API}/{pk}/activate")
        pending = self.client.post(f"{API}/{pk}/revert", {"version": 2}, format="json").data
        self.assertEqual(pending["pending"]["name"], "v2")

    def test_test_transition_and_activation_requires_active_refs(self):
        form_id = self.create().data["id"]
        task = self.client.post(API, {"kind": "task", "name": "Ko‘rik", "data": {"executor": "VET",
                                                                               "form_id": form_id}}, format="json")
        self.assertEqual(task.status_code, 201, task.data)
        response = self.client.post(f"{API}/{task.data['id']}/activate")
        self.assertEqual(response.status_code, 400)
        self.assertIn("faollashtiring", response.data["detail"])
        self.assertEqual(self.client.post(f"{API}/{form_id}/test").data["status"], "test")
        self.assertEqual(self.client.post(f"{API}/{form_id}/test").status_code, 400)  # test only from draft
        self.assertEqual(self.client.post(f"{API}/{task.data['id']}/test").data["status"], "test")

    def test_archive_409_with_references_restore_delete(self):
        form_id = self.create().data["id"]
        self.client.post(f"{API}/{form_id}/activate")
        task = self.client.post(API, {"kind": "task", "name": "T", "data": {"executor": "VET", "form_id": form_id}},
                                format="json").data
        response = self.client.post(f"{API}/{form_id}/archive")
        self.assertEqual(response.status_code, 409)
        detail = self.client.get(f"{API}/{form_id}").data
        self.assertEqual(detail["usage"]["referenced_by"], [{"id": task["id"], "kind": "task", "name": "T"}])
        self.assertEqual(self.client.delete(f"{API}/{form_id}").status_code, 409)  # not a draft
        self.assertEqual(self.client.delete(f"{API}/{task['id']}").status_code, 204)
        self.assertFalse(ConfigVersion.objects.filter(object_type="blueprint", object_id=task["id"]).exists())
        self.assertEqual(self.client.post(f"{API}/{form_id}/archive").data["status"], "archived")
        self.assertEqual(self.client.patch(f"{API}/{form_id}", {"name": "x"}, format="json").status_code, 409)
        self.assertEqual(self.client.post(f"{API}/{form_id}/restore").data["status"], "draft")

    def test_role_profile_archive_blocked_by_users_and_delete_referenced_draft(self):
        profile = self.role_profile({"warehouse": ["view"]})
        UserAccess.objects.create(user=self.warehouse, profile=profile)
        self.as_user(self.superuser)
        self.assertEqual(self.client.post(f"{API}/{profile.pk}/archive").status_code, 409)
        draft_form = self.create().data["id"]
        self.client.post(API, {"kind": "task", "name": "T", "data": {"executor": "VET", "form_id": draft_form}},
                         format="json")
        self.assertEqual(self.client.delete(f"{API}/{draft_form}").status_code, 409)

    def test_list_filters_and_meta(self):
        self.create()
        self.role_profile({"warehouse": ["view"]})
        self.as_user(self.superuser)
        self.assertEqual(self.client.get(API, {"kind": "role"}).data["count"], 1)
        self.assertEqual(self.client.get(API, {"status": "draft"}).data["count"], 1)
        self.assertEqual(self.client.get(API, {"search": "ko‘rigi"}).data["count"], 1)
        meta = self.client.get("/api/builder/meta").data
        self.assertIn("Navobod tumani", meta["districts"])
        self.assertIn({"value": "view", "label": "Ko‘rish"}, meta["actions"])
        self.assertTrue(any(ref["value"].startswith("profile:") for ref in meta["role_refs"]))
        self.assertEqual(len(meta["modules"]), 16)

    def test_role_profile_pending_base_role_change_updates_users(self):
        profile = self.role_profile({"warehouse": ["view"]})
        self.as_user(self.superuser)
        self.client.put(f"/api/builder/user-access/{self.warehouse.pk}", {"profile_id": profile.pk}, format="json")
        version = type(self.warehouse).objects.get(pk=self.warehouse.pk).token_version
        self.client.patch(f"{API}/{profile.pk}", {"data": {"base_role": "VET", "permissions": {}}}, format="json")
        self.warehouse.refresh_from_db()
        self.assertEqual(self.warehouse.role, Role.WAREHOUSE)  # pending only
        self.client.post(f"{API}/{profile.pk}/activate")
        self.warehouse.refresh_from_db()
        self.assertEqual(self.warehouse.role, Role.VET)
        self.assertNotEqual(self.warehouse.token_version, version)
        self.assertEqual(Blueprint.objects.get(pk=profile.pk).status, BlueprintStatus.ACTIVE)

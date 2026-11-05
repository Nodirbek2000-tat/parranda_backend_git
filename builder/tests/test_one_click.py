"""One-click «Saqlash»: create+activate (``activate``) and immediate edit of an active blueprint (``apply``)."""

from __future__ import annotations

from builder.models import Blueprint, BlueprintKind, BlueprintStatus, ConfigVersion

from .helpers import API, BuilderTestCase, number_form


class OneClickSaveTests(BuilderTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.as_user(self.superuser)

    def test_create_with_activate_is_active_at_once(self):
        response = self.client.post(API, {"kind": "form", "name": "Ko‘rik", "data": number_form(), "activate": True},
                                    format="json")
        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(response.data["status"], "active")

    def test_failed_activation_creates_nothing(self):
        form = self.form(activate=False)  # draft → a task template referencing it cannot be activated
        data = {"executor": "VET", "approver": "ADMIN", "form_id": form.pk, "duration_days": 2}
        response = self.client.post(API, {"kind": "task", "name": "Ko‘rik vazifasi", "data": data, "activate": True},
                                    format="json")
        self.assertEqual(response.status_code, 400, response.data)
        self.assertFalse(Blueprint.objects.filter(kind=BlueprintKind.TASK, name="Ko‘rik vazifasi").exists())

    def test_apply_changes_active_immediately_and_keeps_history(self):
        form = self.form()
        response = self.client.patch(f"{API}/{form.pk}", {"name": "Yangi nom", "apply": True}, format="json")
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["name"], "Yangi nom")
        self.assertIsNone(response.data["pending"])
        form.refresh_from_db()
        self.assertEqual((form.status, form.version), (BlueprintStatus.ACTIVE, 2))
        self.assertTrue(ConfigVersion.objects.filter(object_id=form.pk, version=2, name="Yangi nom").exists())

    def test_without_apply_active_edit_still_waits_for_confirmation(self):
        form = self.form()
        response = self.client.patch(f"{API}/{form.pk}", {"name": "Keyinroq"}, format="json")
        self.assertEqual(response.status_code, 200)
        self.assertIsNotNone(response.data["pending"])
        self.assertEqual(Blueprint.objects.get(pk=form.pk).name, form.name)

    def test_apply_drops_parked_pending_change(self):
        form = self.form()
        self.client.patch(f"{API}/{form.pk}", {"description": "kutmoqda"}, format="json")
        response = self.client.patch(f"{API}/{form.pk}", {"name": "Darhol", "apply": True}, format="json")
        self.assertEqual(response.status_code, 200, response.data)
        self.assertIsNone(response.data["pending"])
        self.assertEqual(response.data["name"], "Darhol")

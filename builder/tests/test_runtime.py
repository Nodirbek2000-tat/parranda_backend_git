"""Runtime: forms, tasks, processes and test mode (API_CONTRACT_V4 §1 test mode, §3)."""

from __future__ import annotations

from datetime import timedelta

from django.core.files.uploadedfile import SimpleUploadedFile
from django.utils import timezone

from builder import services
from builder.models import (
    BlueprintKind,
    BlueprintStatus,
    FormSubmission,
    ProcessRun,
    ProcessRunStatus,
    TaskInstance,
    TaskStatus,
)
from common.roles import Role
from core import files
from core.models import Attachment, AttachmentPurpose
from notifications.models import Notification

from .helpers import BuilderTestCase

PNG = b"\x89PNG\r\n\x1a\n" + b"0" * 64


class FormRuntimeTests(BuilderTestCase):
    def test_list_submit_and_scope(self):
        form = self.form()
        self.blueprint(BlueprintKind.FORM, {"roles": ["VET"], "fields": [{"key": "a", "label": "A", "type": "text"}]},
                       name="Faqat vet")
        self.as_user(self.warehouse)
        names = [row["name"] for row in self.client.get("/api/forms").data]
        self.assertEqual(names, ["form namuna"])
        response = self.client.post(f"/api/forms/{form.pk}/submissions",
                                    {"household_id": self.household_a.pk, "data": {"area_m2": 20, "birds": 4}},
                                    format="json")
        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(response.data["code"], f"FM-{response.data['id']:06d}")
        self.assertEqual(response.data["data"]["density"], 50)
        self.assertEqual(response.data["household"]["code"], self.household_a.code)
        self.assertEqual([d["value"] for d in response.data["display"]][:3], ["20", "4", "50"])
        bad = self.client.post(f"/api/forms/{form.pk}/submissions", {"data": {"area_m2": -1}}, format="json")
        self.assertEqual(bad.status_code, 400)
        self.assertEqual(set(bad.data["data"]), {"area_m2", "birds"})
        # own submissions only; ADMIN sees all; OWNER of the household too
        self.as_user(self.vet)
        self.assertEqual(self.client.get("/api/forms/submissions").data["count"], 0)
        self.assertEqual(self.client.get(f"/api/forms/submissions/{response.data['id']}").status_code, 404)
        self.as_user(self.admin)
        self.assertEqual(self.client.get("/api/forms/submissions", {"form": form.pk}).data["count"], 1)
        self.as_user(self.owner_a)
        self.assertEqual(self.client.get("/api/forms/submissions").data["count"], 1)
        self.as_user(self.owner_b)
        self.assertEqual(self.client.get("/api/forms/submissions").data["count"], 0)

    def test_photo_upload_is_bound_to_submission(self):
        data = {"fields": [{"key": "photo", "label": "Rasm", "type": "photo", "required": True}]}
        form = self.blueprint(BlueprintKind.FORM, data, name="Rasmli")
        self.as_user(self.vet)
        upload = self.client.post("/api/files", {"file": SimpleUploadedFile("a.png", PNG, "image/png"),
                                                 "purpose": "FORM_FILE"}, format="multipart")
        self.assertEqual(upload.status_code, 201, upload.data)
        response = self.client.post(f"/api/forms/{form.pk}/submissions", {"data": {"photo": upload.data["id"]}},
                                    format="json")
        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(response.data["display"][0]["file_id"], upload.data["id"])
        self.assertEqual(response.data["display"][0]["value"], "a.png")
        row = Attachment.objects.get(pk=upload.data["id"])
        self.assertEqual((row.object_type, row.object_id), ("form_submission", response.data["id"]))
        self.as_user(self.vet2)
        self.assertEqual(self.client.get(f"/api/files/{row.pk}").status_code, 404)


class TaskFlowTests(BuilderTestCase):
    def create_task(self, template, user=None, **body):
        self.as_user(user or self.admin)
        return self.client.post("/api/tasks", {"template_id": template.pk, **body}, format="json")

    def test_full_flow_with_return_and_resubmission(self):
        data = {"fields": [
            {"key": "area_m2", "label": "Maydon", "type": "number", "required": True},
            {"key": "verdict", "label": "Xulosa", "type": "text", "editable_roles": ["ADMIN"]},
        ]}
        form = self.blueprint(BlueprintKind.FORM, data)
        template = self.task_template(form=form, requires_household=True)
        self.assertEqual(self.create_task(template).data["household_id"], ["Bu maydon to‘ldirilishi shart."])
        response = self.create_task(template, household_id=self.household_a.pk)
        self.assertEqual(response.status_code, 201, response.data)
        task = response.data
        self.assertEqual((task["status"], task["executor_display"]), ("OPEN", "Veterinar"))
        self.assertEqual(task["due_on"], (timezone.localdate() + timedelta(days=3)).isoformat())
        self.assertTrue(Notification.objects.filter(event="task_assigned", recipient=self.vet).exists())
        self.assertTrue(Notification.objects.filter(event="task_assigned", recipient=self.owner_a).exists())

        self.as_user(self.vet)
        self.assertEqual(self.client.get("/api/tasks/counts").data, {"mine_open": 1, "to_approve": 0})
        mine = self.client.get("/api/tasks").data["results"][0]
        self.assertTrue(mine["can_submit"])
        self.assertFalse(mine["can_approve"])
        self.assertEqual([f["key"] for f in mine["form"]["fields"]], ["area_m2", "verdict"])
        url = f"/api/tasks/{task['id']}"
        bad = self.client.post(f"{url}/submit", {"data": {}}, format="json")
        self.assertEqual(bad.status_code, 400)
        self.assertIn("area_m2", bad.data["data"])
        submitted = self.client.post(f"{url}/submit", {"data": {"area_m2": 10, "verdict": "x"}}, format="json")
        self.assertEqual(submitted.data["status"], "SUBMITTED")
        self.assertEqual(submitted.data["submission"]["data"], {"area_m2": 10})  # verdict not editable by VET
        self.assertEqual(self.client.post(f"{url}/approve").status_code, 403)
        self.assertTrue(Notification.objects.filter(event="task_submitted", recipient=self.admin).exists())

        self.as_user(self.admin)
        self.assertEqual(self.client.get("/api/tasks", {"view": "approve"}).data["count"], 1)
        self.assertEqual(self.client.post(f"{url}/return", {}, format="json").status_code, 400)
        returned = self.client.post(f"{url}/return", {"note": "Rasm yo‘q"}, format="json")
        self.assertEqual((returned.data["status"], returned.data["decision_note"]), ("RETURNED", "Rasm yo‘q"))
        self.assertTrue(Notification.objects.filter(event="task_returned", recipient=self.vet).exists())

        self.as_user(self.vet)
        again = self.client.post(f"{url}/submit", {"data": {"area_m2": 12}}, format="json")
        self.assertEqual(again.data["status"], "SUBMITTED")
        self.assertEqual(FormSubmission.objects.filter(task_id=task["id"]).count(), 2)
        self.as_user(self.admin)
        approved = self.client.post(f"{url}/approve", {"note": "ok"}, format="json")
        self.assertEqual(approved.data["status"], "APPROVED")
        self.assertEqual(approved.data["decided_by_name"], self.admin.full_name)
        self.assertEqual(self.client.post(f"{url}/approve").status_code, 409)
        self.assertEqual(self.client.post(f"{url}/cancel").status_code, 409)
        self.assertTrue(Notification.objects.filter(event="task_approved", recipient=self.vet).exists())
        self.as_user(self.owner_a)
        self.assertEqual(self.client.get(url).status_code, 200)
        self.as_user(self.owner_b)
        self.assertEqual(self.client.get(url).status_code, 404)

    def test_editable_value_kept_on_resubmission(self):
        data = {"fields": [{"key": "a", "label": "A", "type": "text"},
                           {"key": "b", "label": "B", "type": "text", "editable_roles": ["VET"]}]}
        template = self.task_template(form=self.blueprint(BlueprintKind.FORM, data), executor=Role.ADMIN)
        task = self.create_task(template).data
        self.as_user(self.admin)
        self.client.post(f"/api/tasks/{task['id']}/submit", {"data": {"a": "1", "b": "admin"}}, format="json")
        sub = FormSubmission.objects.get(task_id=task["id"])
        self.assertEqual(sub.data, {"a": "1"})
        FormSubmission.objects.filter(pk=sub.pk).update(data={"a": "1", "b": "vet value"})
        self.client.post(f"/api/tasks/{task['id']}/return", {"note": "qayta"}, format="json")
        self.client.post(f"/api/tasks/{task['id']}/submit", {"data": {"a": "2", "b": "changed"}}, format="json")
        latest = FormSubmission.objects.filter(task_id=task["id"]).latest("id")
        self.assertEqual(latest.data, {"a": "2", "b": "vet value"})

    def test_no_approver_submit_approves_and_assignee_rules(self):
        template = self.task_template(approver="")
        self.assertEqual(self.create_task(template, assigned_to_id=self.logist.pk).status_code, 400)
        task = self.create_task(template, assigned_to_id=self.vet2.pk).data
        self.assertEqual(task["assigned_to"]["id"], self.vet2.pk)
        self.as_user(self.vet)
        self.assertEqual(self.client.get(f"/api/tasks/{task['id']}").status_code, 404)  # assigned to vet2
        self.as_user(self.vet2)
        done = self.client.post(f"/api/tasks/{task['id']}/submit", {}, format="json")
        self.assertEqual(done.data["status"], "APPROVED")
        self.assertEqual(done.data["decided_by_name"], self.vet2.full_name)
        self.assertIsNone(done.data["form"])

    def test_permissions_cancel_overdue_and_views(self):
        template = self.task_template()
        self.assertEqual(self.create_task(template, user=self.vet).status_code, 403)
        task = self.create_task(template, starts_on="2026-01-01", due_on="2026-01-02").data
        self.assertTrue(task["overdue"])
        self.assertTrue(task["can_cancel"])
        self.assertEqual(self.create_task(template, starts_on="2026-01-05", due_on="2026-01-02").status_code, 400)
        self.as_user(self.vet)
        self.assertEqual(self.client.get("/api/tasks", {"view": "all"}).status_code, 403)
        self.assertEqual(self.client.post(f"/api/tasks/{task['id']}/cancel").status_code, 403)
        self.as_user(self.logist)
        self.assertEqual(self.client.get(f"/api/tasks/{task['id']}").status_code, 404)
        self.as_user(self.admin)
        cancelled = self.client.post(f"/api/tasks/{task['id']}/cancel", {"note": "kerak emas"}, format="json")
        self.assertEqual(cancelled.data["status"], "CANCELLED")
        self.assertEqual(self.client.get("/api/tasks", {"view": "all"}).data["count"], 1)

    def test_profile_executor(self):
        profile = self.role_profile({"tasks": ["view", "create"]}, base_role=Role.WAREHOUSE)
        services.set_user_access(actor=self.superuser, user_id=self.warehouse.pk, profile_id=profile.pk,
                                 districts=[], household_ids=[])
        template = self.task_template(executor=f"profile:{profile.pk}", approver="")
        task = self.create_task(template).data
        self.assertEqual(task["executor_display"], "Katta omborchi")
        self.assertTrue(Notification.objects.filter(event="task_assigned", recipient=self.warehouse).exists())
        self.as_user(self.warehouse)
        self.assertEqual(self.client.get("/api/tasks").data["count"], 1)
        self.as_user(self.vet)
        self.assertEqual(self.client.get("/api/tasks").data["count"], 0)


class TestModeTests(BuilderTestCase):
    def test_test_blueprints_only_for_superusers(self):
        form = services.create_blueprint(actor=self.superuser, kind=BlueprintKind.FORM, name="Sinov forma",
                                         data={"fields": [{"key": "a", "label": "A", "type": "text"}]})
        services.test_blueprint(actor=self.superuser, pk=form.pk)
        template = self.task_template(activate=False, form=form)
        services.test_blueprint(actor=self.superuser, pk=template.pk)
        self.as_user(self.admin)
        self.assertEqual(self.client.get("/api/forms").data, [])
        self.assertEqual(self.client.post("/api/tasks", {"template_id": template.pk}, format="json").status_code, 400)
        self.as_user(self.superuser)
        self.assertEqual([f["name"] for f in self.client.get("/api/forms").data], ["Sinov forma"])
        task = self.client.post("/api/tasks", {"template_id": template.pk}, format="json")
        self.assertEqual(task.status_code, 201, task.data)
        self.assertTrue(task.data["is_test"])
        self.assertFalse(Notification.objects.filter(event="task_assigned").exists())  # test rows notify nobody
        submitted = self.client.post(f"/api/tasks/{task.data['id']}/submit", {"data": {"a": "x"}}, format="json")
        self.assertEqual(submitted.data["status"], "SUBMITTED")
        self.assertTrue(FormSubmission.objects.get(task_id=task.data["id"]).is_test)
        self.as_user(self.admin)
        self.assertEqual(self.client.get("/api/tasks", {"view": "all"}).data["count"], 0)
        self.assertEqual(self.client.get(f"/api/tasks/{task.data['id']}").status_code, 404)
        self.assertEqual(self.client.get("/api/forms/submissions").data["count"], 0)


class ProcessTests(BuilderTestCase):
    def make_process(self, *, conditions=("approved", "submitted"), notify=("ADMIN",)):
        first = self.task_template(name="Qurilish", executor=Role.VET, requires_household=True)
        second = self.task_template(name="Ombor", executor=Role.WAREHOUSE, approver=Role.ADMIN)
        third = self.task_template(name="Yakun", executor=Role.ADMIN, approver="")
        stages = [{"title": t.name, "task_id": t.pk, "notify_roles": list(notify), "condition": c}
                  for t, c in zip((first, second, third), (*conditions, ""))]
        return self.blueprint(BlueprintKind.PROCESS, {"stages": stages}, name="Sikl"), (first, second, third)

    def start(self, process, **body):
        self.as_user(self.admin)
        return self.client.post("/api/process-runs", {"process_id": process.pk, **body}, format="json")

    def test_advance_to_done(self):
        process, (first, second, third) = self.make_process()
        self.assertEqual(self.start(process).data["household_id"], ["Bu maydon to‘ldirilishi shart."])
        run = self.start(process, household_id=self.household_a.pk)
        self.assertEqual(run.status_code, 201, run.data)
        self.assertEqual(run.data["code"], f"JR-{run.data['id']:06d}")
        self.assertEqual([s["state"] for s in run.data["stages"]], ["current", "pending", "pending"])
        stage0 = TaskInstance.objects.get(process_run_id=run.data["id"], stage_index=0)
        self.assertEqual(stage0.household_id, self.household_a.pk)
        self.as_user(self.vet)
        self.assertEqual(self.client.get("/api/process-runs").data["count"], 1)
        self.assertEqual(self.client.get(f"/api/tasks/{stage0.pk}").data["process_run"]["stage_title"], "Qurilish")
        self.client.post(f"/api/tasks/{stage0.pk}/submit", {}, format="json")
        self.assertEqual(ProcessRun.objects.get(pk=run.data["id"]).current_index, 0)  # needs approval
        self.as_user(self.admin)
        self.client.post(f"/api/tasks/{stage0.pk}/approve", {}, format="json")
        stage1 = TaskInstance.objects.get(process_run_id=run.data["id"], stage_index=1)
        self.assertTrue(Notification.objects.filter(event="process_stage_started", recipient=self.admin).exists())
        self.as_user(self.warehouse)
        self.client.post(f"/api/tasks/{stage1.pk}/submit", {}, format="json")  # condition "submitted"
        stage2 = TaskInstance.objects.get(process_run_id=run.data["id"], stage_index=2)
        self.as_user(self.admin)
        self.client.post(f"/api/tasks/{stage2.pk}/submit", {}, format="json")
        detail = self.client.get(f"/api/process-runs/{run.data['id']}").data
        self.assertEqual(detail["status"], "DONE")
        self.assertEqual([s["state"] for s in detail["stages"]], ["done", "done", "done"])
        self.assertIsNotNone(detail["finished_at"])
        self.assertTrue(Notification.objects.filter(event="process_finished", recipient=self.admin).exists())
        self.as_user(self.owner_a)
        self.assertEqual(self.client.get("/api/process-runs").data["count"], 1)
        self.as_user(self.owner_b)
        self.assertEqual(self.client.get("/api/process-runs").data["count"], 0)

    def test_cancel_cancels_open_tasks(self):
        process, _ = self.make_process()
        run = self.start(process, household_id=self.household_a.pk).data
        self.as_user(self.vet)
        self.assertEqual(self.client.post(f"/api/process-runs/{run['id']}/cancel").status_code, 403)
        self.as_user(self.admin)
        cancelled = self.client.post(f"/api/process-runs/{run['id']}/cancel", {"reason": "to‘xtatildi"},
                                     format="json").data
        self.assertEqual(cancelled["status"], "CANCELLED")
        self.assertEqual(cancelled["stages"][0]["task"]["status"], "CANCELLED")
        self.assertEqual(self.client.post(f"/api/process-runs/{run['id']}/cancel").status_code, 409)

    def test_blocked_stage(self):
        process, (first, second, _third) = self.make_process()
        run = self.start(process, household_id=self.household_a.pk).data
        # the archived-template situation (e.g. data edited outside the API): the run must stay RUNNING
        type(second).objects.filter(pk=second.pk).update(status=BlueprintStatus.ARCHIVED)
        stage0 = TaskInstance.objects.get(process_run_id=run["id"], stage_index=0)
        self.as_user(self.vet)
        self.client.post(f"/api/tasks/{stage0.pk}/submit", {}, format="json")
        self.as_user(self.admin)
        self.client.post(f"/api/tasks/{stage0.pk}/approve", {}, format="json")
        row = ProcessRun.objects.get(pk=run["id"])
        self.assertEqual((row.status, row.current_index), (ProcessRunStatus.RUNNING, 1))
        self.assertFalse(TaskInstance.objects.filter(process_run=row, stage_index=1).exists())
        self.assertTrue(Notification.objects.filter(event="process_blocked", recipient=self.admin).exists())
        self.assertEqual(TaskInstance.objects.get(pk=stage0.pk).status, TaskStatus.APPROVED)

    def test_task_template_in_process_cannot_be_archived(self):
        process, (first, _s, _t) = self.make_process()
        self.as_user(self.superuser)
        self.assertEqual(self.client.post(f"/api/builder/blueprints/{first.pk}/archive").status_code, 409)
        self.assertEqual(self.client.get(f"/api/builder/blueprints/{process.pk}").data["usage"]["runs"], 0)


class FileUploadPurposeTests(BuilderTestCase):
    def test_every_role_may_upload_form_files(self):
        for user in (self.owner_a, self.vet, self.logist, self.warehouse):
            row = files.upload(actor=user, file=SimpleUploadedFile("x.png", PNG, "image/png"),
                               purpose=AttachmentPurpose.FORM_FILE)
            self.assertEqual(row.purpose, "FORM_FILE")

"""Soft delete of tasks (``DELETE /api/tasks/{id}``) and process runs (``DELETE /api/process-runs/{id}``)."""

from __future__ import annotations

from audit.models import AuditAction, AuditLog
from builder import services
from builder.models import BlueprintKind, FormSubmission, ProcessRun, TaskInstance, TaskStatus
from common.roles import Role

from .helpers import BuilderTestCase


def _ids(response) -> list[int]:
    return [row["id"] for row in response.data["results"]]


class TaskDeleteTests(BuilderTestCase):
    def make_task(self):
        form = self.form()
        template = self.task_template(form=form)
        self.as_user(self.admin)
        response = self.client.post("/api/tasks", {"template_id": template.pk}, format="json")
        self.assertEqual(response.status_code, 201, response.data)
        return response.data

    def test_admin_deletes_open_task_history_kept(self):
        task = self.make_task()
        self.as_user(self.vet)
        submitted = self.client.post(f"/api/tasks/{task['id']}/submit",
                                     {"data": {"area_m2": 10, "birds": 5}}, format="json")
        self.assertEqual(submitted.status_code, 200, submitted.data)
        self.assertEqual(self.client.delete(f"/api/tasks/{task['id']}").status_code, 403)

        self.as_user(self.admin)
        self.assertEqual(self.client.get("/api/tasks/counts").data["to_approve"], 1)
        self.assertEqual(self.client.delete(f"/api/tasks/{task['id']}").status_code, 204)
        self.assertEqual(self.client.get(f"/api/tasks/{task['id']}").status_code, 404)
        self.assertNotIn(task["id"], _ids(self.client.get("/api/tasks?view=all")))
        self.assertEqual(self.client.get("/api/tasks/counts").data["to_approve"], 0)
        self.assertEqual(self.client.post(f"/api/tasks/{task['id']}/approve", {}, format="json").status_code, 404)
        self.assertEqual(self.client.delete(f"/api/tasks/{task['id']}").status_code, 404)
        self.as_user(self.vet)
        self.assertNotIn(task["id"], _ids(self.client.get("/api/tasks?view=mine")))

        row = TaskInstance.all_objects.get(pk=task["id"])
        self.assertIsNotNone(row.deleted_at)
        self.assertEqual(row.status, TaskStatus.SUBMITTED)  # nothing else changed
        submission = FormSubmission.objects.get(task_id=task["id"])
        self.assertEqual(submission.task.pk, task["id"])  # history FK still loads
        self.assertTrue(AuditLog.objects.filter(module="builder", action=AuditAction.DELETE,
                                                object_id=task["id"]).exists())
        # the template stays "used" (soft-deleted tasks still reference it)
        self.assertEqual(services.usage_map([row.template])[row.template_id]["tasks"], 1)

    def test_owner_and_staff_cannot_delete(self):
        task = self.make_task()
        for user in (self.vet, self.warehouse, self.owner_a, self.logist):
            self.as_user(user)
            self.assertEqual(self.client.delete(f"/api/tasks/{task['id']}").status_code, 403, user)
        self.assertIsNone(TaskInstance.all_objects.get(pk=task["id"]).deleted_at)


class ProcessRunDeleteTests(BuilderTestCase):
    def make_process(self):
        first = self.task_template(name="Qurilish", executor=Role.VET)
        second = self.task_template(name="Ombor", executor=Role.WAREHOUSE, approver=Role.ADMIN)
        third = self.task_template(name="Yakun", executor=Role.ADMIN, approver="")
        stages = [{"title": t.name, "task_id": t.pk, "notify_roles": [], "condition": c}
                  for t, c in zip((first, second, third), ("approved", "submitted", ""))]
        return self.blueprint(BlueprintKind.PROCESS, {"stages": stages}, name="Sikl")

    def start(self, process) -> dict:
        self.as_user(self.admin)
        response = self.client.post("/api/process-runs", {"process_id": process.pk}, format="json")
        self.assertEqual(response.status_code, 201, response.data)
        return response.data

    def test_running_run_and_its_tasks_are_refused_then_cancel_and_delete(self):
        run = self.start(self.make_process())
        stage0 = TaskInstance.objects.get(process_run_id=run["id"], stage_index=0)
        self.as_user(self.admin)
        response = self.client.delete(f"/api/process-runs/{run['id']}")
        self.assertEqual(response.status_code, 409)
        self.assertIn("bekor qiling", response.data["detail"])
        response = self.client.delete(f"/api/tasks/{stage0.pk}")
        self.assertEqual(response.status_code, 409)
        self.assertIn(run["code"], response.data["detail"])

        self.as_user(self.vet)
        self.assertEqual(self.client.delete(f"/api/process-runs/{run['id']}").status_code, 403)
        self.as_user(self.admin)
        self.assertEqual(self.client.post(f"/api/process-runs/{run['id']}/cancel", {"reason": "kerak emas"},
                                          format="json").status_code, 200)
        self.assertEqual(self.client.delete(f"/api/process-runs/{run['id']}").status_code, 204)
        self.assertEqual(self.client.get(f"/api/process-runs/{run['id']}").status_code, 404)
        self.assertNotIn(run["id"], _ids(self.client.get("/api/process-runs")))
        self.assertEqual(self.client.get(f"/api/tasks/{stage0.pk}").status_code, 404)  # its tasks are hidden too
        self.assertNotIn(stage0.pk, _ids(self.client.get("/api/tasks?view=all")))
        self.assertEqual(self.client.delete(f"/api/process-runs/{run['id']}").status_code, 404)

        stage0 = TaskInstance.all_objects.get(pk=stage0.pk)
        self.assertIsNotNone(stage0.deleted_at)
        self.assertEqual(stage0.process_run.pk, run["id"])  # history FK still loads the deleted run
        self.assertIsNotNone(ProcessRun.all_objects.get(pk=run["id"]).deleted_at)
        self.assertTrue(AuditLog.objects.filter(module="builder", action=AuditAction.DELETE,
                                                object_id=run["id"]).exists())

    def test_done_run_with_an_unapproved_task_is_refused(self):
        run = self.start(self.make_process())
        stage0 = TaskInstance.objects.get(process_run_id=run["id"], stage_index=0)
        self.as_user(self.vet)
        self.client.post(f"/api/tasks/{stage0.pk}/submit", {}, format="json")
        self.as_user(self.admin)
        self.client.post(f"/api/tasks/{stage0.pk}/approve", {}, format="json")
        stage1 = TaskInstance.objects.get(process_run_id=run["id"], stage_index=1)
        self.as_user(self.warehouse)
        self.client.post(f"/api/tasks/{stage1.pk}/submit", {}, format="json")  # "submitted" → next stage
        stage2 = TaskInstance.objects.get(process_run_id=run["id"], stage_index=2)
        self.as_user(self.admin)
        self.client.post(f"/api/tasks/{stage2.pk}/submit", {}, format="json")
        self.assertEqual(self.client.get(f"/api/process-runs/{run['id']}").data["status"], "DONE")

        response = self.client.delete(f"/api/process-runs/{run['id']}")
        self.assertEqual(response.status_code, 409)
        self.assertIn(stage1.code, response.data["detail"])
        self.client.post(f"/api/tasks/{stage1.pk}/approve", {}, format="json")
        self.assertEqual(self.client.delete(f"/api/process-runs/{run['id']}").status_code, 204)
        self.assertFalse(TaskInstance.objects.filter(process_run_id=run["id"]).exists())
        self.assertEqual(TaskInstance.all_objects.filter(process_run_id=run["id"]).count(), 3)

    def test_single_task_of_a_finished_run_can_be_deleted(self):
        run = self.start(self.make_process())
        stage0 = TaskInstance.objects.get(process_run_id=run["id"], stage_index=0)
        self.as_user(self.admin)
        self.client.post(f"/api/process-runs/{run['id']}/cancel", {}, format="json")
        self.assertEqual(self.client.delete(f"/api/tasks/{stage0.pk}").status_code, 204)
        self.assertEqual(self.client.get(f"/api/process-runs/{run['id']}").status_code, 200)

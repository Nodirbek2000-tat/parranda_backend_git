"""Soft delete of acts (``DELETE /api/acts/{id}``) and acts content without deleted vet records / tasks."""

from __future__ import annotations

from audit.models import AuditAction, AuditLog
from builder.models import FormSubmission, TaskInstance
from vet.models import DiseaseRecord, Vaccination, VetRequest, VetVisit

from .helpers import ActsTestCase


def _ids(response) -> list[int]:
    return [row["id"] for row in response.data["results"]]


class ActDeleteTests(ActsTestCase):
    def test_admin_deletes_act_and_it_disappears(self):
        created = self.create(self.admin, kind="role", role="VET", user_id=self.vet.pk)
        self.assertEqual(created.status_code, 201, created.data)
        pk = created.data["id"]
        self.login_as(self.admin)
        response = self.client.delete(f"/api/acts/{pk}")
        self.assertEqual(response.status_code, 204)
        self.assertNotIn(pk, _ids(self.client.get("/api/acts")))
        self.assertEqual(self.client.get(f"/api/acts/{pk}").status_code, 404)
        self.assertEqual(self.client.get(f"/api/acts/{pk}/pdf").status_code, 404)
        self.assertEqual(self.client.delete(f"/api/acts/{pk}").status_code, 404)
        from acts.models import Act

        row = Act.all_objects.get(pk=pk)
        self.assertIsNotNone(row.deleted_at)
        self.assertTrue(row.snapshot)  # frozen content is kept
        self.assertTrue(AuditLog.objects.filter(module="acts", action=AuditAction.DELETE, object_id=pk).exists())

    def test_only_admin_may_delete(self):
        created = self.create(self.vet, kind="task", task_id=self.task.pk)
        self.assertEqual(created.status_code, 201, created.data)
        pk = created.data["id"]
        for user in (self.vet, self.owner_a):  # both may open the act
            self.login_as(user)
            self.assertEqual(self.client.get(f"/api/acts/{pk}").status_code, 200)
            self.assertEqual(self.client.delete(f"/api/acts/{pk}").status_code, 403)
        self.login_as(self.admin)
        self.assertEqual(self.client.delete(f"/api/acts/{pk}").status_code, 204)

    def test_acts_of_a_deleted_task_still_load_it(self):
        created = self.create(self.admin, kind="task", task_id=self.task.pk)
        self.assertEqual(created.status_code, 201, created.data)
        TaskInstance.all_objects.filter(pk=self.task.pk).update(deleted_at=self.task.created_at)
        self.login_as(self.admin)
        response = self.client.get(f"/api/acts/{created.data['id']}")
        self.assertEqual(response.status_code, 200)
        from acts.models import Act

        self.assertEqual(Act.objects.get(pk=created.data["id"]).task.pk, self.task.pk)
        # a new act can no longer be made for the deleted task
        self.assertEqual(self.preview(self.admin, kind="task", task_id=self.task.pk).status_code, 404)


class ActContentSkipsDeletedTests(ActsTestCase):
    def test_vet_act_ignores_deleted_vet_records_and_tasks(self):
        self.login_as(self.admin)
        visit = VetVisit.objects.get(pk=self.visit.pk)
        vaccination = Vaccination.objects.get(household=self.household_a, vaccine_name="Nyukasl")
        disease = DiseaseRecord.objects.get(disease_name="Kolibakterioz")
        request = VetRequest.objects.get(household=self.household_a)
        for url in (f"/api/vet/visits/{visit.pk}", f"/api/vet/vaccinations/{vaccination.pk}",
                    f"/api/vet/diseases/{disease.pk}", f"/api/vet/requests/{request.pk}",
                    f"/api/tasks/{self.task.pk}"):
            self.assertEqual(self.client.delete(url).status_code, 204, url)
        snap = self.preview(self.admin, kind="role", role="VET", user_id=self.vet.pk).data
        summary = self.summary(snap)
        for label in ("O‘tkazilgan tekshiruvlar", "O‘tkazilgan emlashlar", "Kasallik qaydlari",
                      "Yopilgan murojaatlar", "Tasdiqlangan vazifalar"):
            self.assertEqual(summary[label], "0", label)
        self.assertEqual(snap["tasks"], [])
        # the submission of the deleted task still loads it through the FK
        self.assertEqual(FormSubmission.objects.filter(task_id=self.task.pk).first().task.pk, self.task.pk)

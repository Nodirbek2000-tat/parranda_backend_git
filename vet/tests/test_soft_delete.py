"""Soft delete of visits, vaccinations, disease records and owner requests (``DELETE`` on the detail routes).

The row stays as history (``all_objects``), disappears from lists / details / calendar / summary / my-vet /
search / reminders, keeps its stock OUT (a DONE vaccination) and linked rows still load it through the FK.
"""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

from audit.models import AuditAction, AuditLog
from notifications.models import Notification
from warehouse.models import StockTransaction

from .. import reminders
from ..models import DiseaseRecord, Treatment, Vaccination, VetRequest, VetVisit
from .test_api import VetBase, today


def _ids(response) -> list[int]:
    data = response.data
    rows = data["results"] if isinstance(data, dict) and "results" in data else data
    return [row["id"] for row in rows]


class VisitDeleteTests(VetBase):
    def test_vet_deletes_own_visit_and_it_disappears(self):
        visit_id = self.plan_visit().data["id"]
        self.assertEqual(self.client.get("/api/vet/summary").data["visits"]["today"], 1)
        response = self.client.delete(f"/api/vet/visits/{visit_id}")
        self.assertEqual(response.status_code, 204)

        self.assertEqual(self.client.get(f"/api/vet/visits/{visit_id}").status_code, 404)
        self.assertNotIn(visit_id, _ids(self.client.get("/api/vet/visits")))
        self.assertEqual(self.client.get("/api/vet/summary").data["visits"]["today"], 0)
        day = next(d for d in self.client.get("/api/vet/calendar").data if d["date"] == today().isoformat())
        self.assertEqual(day["visits"], [])
        search = self.client.get("/api/search?q=Rejali").data["groups"]
        self.assertFalse(any(g["type"] == "vet_visit" and g["items"] for g in search))
        self.assertEqual(self.client.post(f"/api/vet/visits/{visit_id}/cancel", {"reason": "x"},
                                          format="json").status_code, 404)
        self.assertEqual(self.client.delete(f"/api/vet/visits/{visit_id}").status_code, 404)
        self.login_as(self.owner_a)
        self.assertEqual(self.client.get("/api/my/vet").data["upcoming_visits"], [])

        row = VetVisit.all_objects.get(pk=visit_id)
        self.assertIsNotNone(row.deleted_at)
        self.assertTrue(AuditLog.objects.filter(module="vet", action=AuditAction.DELETE, object_id=visit_id).exists())

    def test_disease_of_a_deleted_visit_still_loads_it(self):
        visit_id = self.plan_visit().data["id"]
        disease = DiseaseRecord.objects.create(household=self.household_a, visit_id=visit_id, disease_name="ND",
                                               diagnosed_date=today(), cases=2, vet=self.vet, created_by=self.vet)
        self.assertEqual(self.client.delete(f"/api/vet/visits/{visit_id}").status_code, 204)
        self.assertEqual(DiseaseRecord.objects.get(pk=disease.pk).visit.pk, visit_id)
        self.assertEqual(self.client.get(f"/api/vet/diseases/{disease.pk}").status_code, 200)

    def test_permissions(self):
        visit_id = self.plan_visit().data["id"]  # assigned to vet1
        self.login_as(self.vet2)
        response = self.client.delete(f"/api/vet/visits/{visit_id}")
        self.assertEqual(response.status_code, 403)
        self.assertIn("boshqa veterinar", response.data["detail"])
        for user in (self.owner_a, self.warehouse, self.logistics):
            self.login_as(user)
            self.assertIn(self.client.delete(f"/api/vet/visits/{visit_id}").status_code, (403, 404), user)
        self.assertIsNone(VetVisit.all_objects.get(pk=visit_id).deleted_at)
        self.login_as(self.admin)
        self.assertEqual(self.client.delete(f"/api/vet/visits/{visit_id}").status_code, 204)

    def test_unassigned_row_only_by_its_author(self):
        self.login_as(self.admin)
        visit_id = self.plan_visit().data["id"]
        self.assertIsNone(VetVisit.objects.get(pk=visit_id).vet_id)
        self.login_as(self.vet)
        self.assertEqual(self.client.delete(f"/api/vet/visits/{visit_id}").status_code, 403)
        own = VetVisit.objects.create(household=self.household_a, planned_date=today(), purpose="X",
                                      created_by=self.vet)
        self.assertEqual(self.client.delete(f"/api/vet/visits/{own.pk}").status_code, 204)


class VaccinationDeleteTests(VetBase):
    def test_done_vaccination_keeps_its_stock_out(self):
        self.stock_in(self.vaccine, 10)
        vaccination_id = self.plan_vaccination().data["id"]
        body = {"done_date": today().isoformat(), "birds_vaccinated": 295, "quantity_used": "3", "result": "Yaxshi"}
        self.assertEqual(self.post(f"/api/vet/vaccinations/{vaccination_id}/complete", body).status_code, 200)
        tx_id = Vaccination.objects.get(pk=vaccination_id).stock_transaction_id
        tx_count = StockTransaction.objects.count()

        self.assertEqual(self.client.delete(f"/api/vet/vaccinations/{vaccination_id}").status_code, 204)
        self.assertEqual(self.client.get(f"/api/vet/vaccinations/{vaccination_id}").status_code, 404)
        self.assertNotIn(vaccination_id, _ids(self.client.get("/api/vet/vaccinations")))
        self.vaccine.refresh_from_db()
        self.assertEqual(self.vaccine.balance, Decimal("7"))  # ledger untouched
        self.assertEqual(StockTransaction.objects.count(), tx_count)
        tx = StockTransaction.objects.get(pk=tx_id)
        self.assertEqual(tx.vaccination.pk, vaccination_id)  # reverse one-to-one still loads the hidden row
        self.login_as(self.admin)
        response = self.client.get("/api/stock-transactions?page_size=200")
        self.assertEqual(response.status_code, 200)
        self.assertIn(tx_id, _ids(response))  # the OUT stays in the ledger
        self.assertEqual(self.client.get("/api/dashboard/admin").status_code, 200)
        self.assertEqual(self.client.get(f"/api/vet/vaccinations?batch={self.batch.pk}").status_code, 200)

    def test_reminders_skip_deleted_rows(self):
        current = today()
        due = Vaccination.objects.create(household=self.household_a, vaccine_name="ND", vet=self.vet,
                                         planned_date=current + timedelta(days=1))
        late = Vaccination.objects.create(household=self.household_a, vaccine_name="ND", vet=self.vet,
                                          planned_date=current - timedelta(days=1))
        visit = VetVisit.objects.create(household=self.household_a, vet=self.vet, planned_date=current,
                                        purpose="Ko‘rik")
        for url in (f"/api/vet/vaccinations/{due.pk}", f"/api/vet/vaccinations/{late.pk}",
                    f"/api/vet/visits/{visit.pk}"):
            self.assertEqual(self.client.delete(url).status_code, 204, url)
        Notification.objects.all().delete()
        queued = (reminders.vaccination_due_job(current) + reminders.vaccination_overdue_job(current)
                  + reminders.vet_visit_today_job(current))
        self.assertEqual(queued, 0)
        self.assertFalse(Notification.objects.exists())

    def test_warehouse_cannot_delete(self):
        vaccination_id = self.plan_vaccination().data["id"]
        self.login_as(self.warehouse)
        self.assertEqual(self.client.delete(f"/api/vet/vaccinations/{vaccination_id}").status_code, 403)


class DiseaseDeleteTests(VetBase):
    def test_delete_disease_treatments_keep_it(self):
        response = self.post("/api/vet/diseases", {"household": self.household_a.pk, "disease_name": "Nyukasl",
                                                   "diagnosed_date": today().isoformat(), "cases": 3})
        self.assertEqual(response.status_code, 201, response.data)
        disease_id = response.data["id"]
        treatment = Treatment.objects.create(household=self.household_a, disease_id=disease_id, vet=self.vet,
                                             drug_name="Enro", start_date=today(), created_by=self.vet)
        self.assertEqual(self.client.delete(f"/api/vet/diseases/{disease_id}").status_code, 204)
        self.assertEqual(self.client.get(f"/api/vet/diseases/{disease_id}").status_code, 404)
        self.assertNotIn(disease_id, _ids(self.client.get("/api/vet/diseases")))
        self.assertEqual(self.client.get("/api/vet/summary").data["diseases"]["open"], 0)
        self.assertEqual(Treatment.objects.get(pk=treatment.pk).disease.pk, disease_id)
        self.assertEqual(self.client.get(f"/api/vet/treatments/{treatment.pk}").status_code, 200)
        self.login_as(self.admin)
        self.assertEqual(self.client.get(f"/api/households/{self.household_a.pk}").status_code, 200)
        self.login_as(self.owner_a)
        self.assertEqual(self.client.get("/api/my/vet").data["open_diseases"], [])


class RequestDeleteTests(VetBase):
    def test_only_admin_deletes_requests(self):
        self.login_as(self.owner_a)
        request_id = self.post("/api/vet/requests", {"text": "Tovuqlar kasal ko‘rinadi"}).data["id"]
        self.assertEqual(self.client.delete(f"/api/vet/requests/{request_id}").status_code, 403)
        self.login_as(self.vet)
        self.assertEqual(self.client.delete(f"/api/vet/requests/{request_id}").status_code, 403)
        visit = self.post(f"/api/vet/requests/{request_id}/plan-visit",
                          {"planned_date": today().isoformat(), "purpose": "Murojaat bo‘yicha"})
        self.assertEqual(visit.status_code, 201, visit.data)

        self.login_as(self.admin)
        self.assertEqual(self.client.delete(f"/api/vet/requests/{request_id}").status_code, 204)
        self.assertEqual(self.client.get(f"/api/vet/requests/{request_id}").status_code, 404)
        self.assertNotIn(request_id, _ids(self.client.get("/api/vet/requests")))
        self.assertEqual(self.client.get("/api/vet/summary").data["requests"]["new"], 0)
        # the visit planned from it stays and still loads the request
        self.assertEqual(VetVisit.objects.get(pk=visit.data["id"]).request.pk, request_id)
        self.assertEqual(self.client.get(f"/api/vet/visits/{visit.data['id']}").status_code, 200)
        self.login_as(self.owner_a)
        self.assertEqual(self.client.get("/api/my/vet").data["requests"], [])
        # kept as history
        self.assertIsNotNone(VetRequest.all_objects.get(pk=request_id).deleted_at)

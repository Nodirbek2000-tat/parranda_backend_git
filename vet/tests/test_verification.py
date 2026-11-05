"""Extra checks of the vet module found in the independent verification (API_CONTRACT_V3 §1.2, §8, §9.4, §13, §14.2)."""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal
from unittest import mock

from audit.models import AuditLog
from common.roles import Role
from common.testing import BOT_API_TOKEN, make_user
from notifications.models import Notification
from warehouse.models import Product, StockTransaction

from ..models import RequestStatus, Treatment, Vaccination, VetRequest, VetStatus, VetVisit
from .test_api import VetBase, today


class RoleAndIsolationTests(VetBase):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.request_a = VetRequest.objects.create(household=cls.household_a, created_by=cls.owner_a, text="Kasal tovuq")
        cls.visit_a = VetVisit.objects.create(
            household=cls.household_a, vet=cls.vet, planned_date=today(), purpose="Ko‘rik", created_by=cls.admin
        )

    def test_owner_cannot_act_as_staff_on_own_request(self):
        self.login_as(self.owner_a)
        self.assertEqual(self.post(f"/api/vet/requests/{self.request_a.pk}/take").status_code, 403)
        body = {"planned_date": today().isoformat(), "purpose": "O‘zim rejalayman"}
        self.assertEqual(self.post(f"/api/vet/requests/{self.request_a.pk}/plan-visit", body).status_code, 403)
        self.assertEqual(self.post(f"/api/vet/visits/{self.visit_a.pk}/complete",
                                   {"health_status": "GOOD", "conclusion": "Yaxshi"}).status_code, 403)
        self.assertEqual(self.patch(f"/api/vet/visits/{self.visit_a.pk}", {"purpose": "Boshqa"}).status_code, 403)
        self.assertEqual(self.client.get("/api/vet/summary").status_code, 403)

    def test_logistics_and_warehouse_cannot_write(self):
        for user in (self.logistics, self.warehouse):
            self.login_as(user)
            self.assertEqual(self.post(f"/api/vet/visits/{self.visit_a.pk}/cancel", {"reason": "Bekor"}).status_code,
                             403)
            self.assertEqual(self.client.get(f"/api/vet/visits/{self.visit_a.pk}").status_code, 403)
            self.assertEqual(self.client.get("/api/my/vet").status_code, 403)
        self.login_as(self.warehouse)
        self.assertEqual(self.client.get("/api/vet/vaccinations").status_code, 403)  # without ?batch=

    def test_owner_without_household_gets_403_on_my_vet(self):
        self.login_as(make_user("owner_nohh", Role.OWNER))
        self.assertEqual(self.client.get("/api/my/vet").status_code, 403)
        self.assertEqual(self.post("/api/vet/requests", {"text": "Yordam kerak"}).status_code, 403)

    def test_unknown_ids_are_404(self):
        for path in ("/api/vet/visits/999999", "/api/vet/vaccinations/999999", "/api/vet/diseases/999999",
                     "/api/vet/treatments/999999", "/api/vet/requests/999999"):
            self.assertEqual(self.client.get(path).status_code, 404, path)

    def test_admin_closes_request_and_plan_visit_on_closed_request_is_rejected(self):
        self.login_as(self.admin)
        response = self.post(f"/api/vet/requests/{self.request_a.pk}/close", {"response": "Telefonda maslahat berildi"})
        self.assertEqual((response.status_code, response.data["status"]), (200, "CLOSED"))
        self.assertEqual(response.data["closed_by"]["id"], self.admin.pk)
        self.assertTrue(Notification.objects.filter(event="vet_request_updated", recipient=self.owner_a).exists())
        log = AuditLog.objects.get(object_type="VetRequest", action="status_change")
        self.assertEqual((log.before["status"], log.after["status"]), ("NEW", "CLOSED"))
        self.login_as(self.vet)
        body = {"planned_date": today().isoformat(), "purpose": "Kech qolgan ko‘rik"}
        response = self.post(f"/api/vet/requests/{self.request_a.pk}/plan-visit", body)
        self.assertEqual(response.status_code, 400)
        self.assertIn("request", response.data)
        self.assertEqual(self.post(f"/api/vet/requests/{self.request_a.pk}/close", {"response": "Yana"}).status_code,
                         409)
        self.assertEqual(VetVisit.objects.filter(request=self.request_a).count(), 0)

    def test_visit_request_from_other_household_is_rejected(self):
        response = self.plan_visit(household=self.household_b.pk, request=self.request_a.pk)
        self.assertEqual(response.status_code, 400)
        self.assertIn("request", response.data)
        self.assertEqual(VetRequest.objects.get(pk=self.request_a.pk).status, RequestStatus.NEW)


class VisitVetChangeTests(VetBase):
    def test_vet_change_notifies_new_vet_with_new_key(self):
        self.login_as(self.admin)
        visit_id = self.plan_visit(vet=self.vet.pk).data["id"]
        response = self.patch(f"/api/vet/visits/{visit_id}", {"vet": self.vet2.pk})
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["vet"]["id"], self.vet2.pk)
        self.assertTrue(Notification.objects.filter(event="vet_visit_planned", recipient=self.vet2).exists())
        self.assertEqual(Notification.objects.filter(event="vet_visit_planned", recipient=self.owner_a).count(), 2)
        self.assertEqual(self.patch(f"/api/vet/visits/{visit_id}", {"vet": self.admin.pk}).status_code, 400)
        # the old vet may no longer conclude it
        self.login_as(self.vet)
        response = self.post(f"/api/vet/visits/{visit_id}/complete", {"health_status": "GOOD", "conclusion": "Yaxshi"})
        self.assertEqual(response.status_code, 403)


class VaccinationEditTests(VetBase):
    def test_edit_with_unchanged_inactive_product_is_allowed(self):
        vaccination_id = self.plan_vaccination().data["id"]
        Product.objects.filter(pk=self.vaccine.pk).update(is_active=False)
        new_date = (today() + timedelta(days=5)).isoformat()
        response = self.patch(f"/api/vet/vaccinations/{vaccination_id}",
                              {"planned_date": new_date, "product": self.vaccine.pk, "batch": None, "dose": "1 ml"})
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual((response.data["planned_date"], response.data["dose"]), (new_date, "1 ml"))
        other = Product.objects.create(name="Gumboro", category="VACCINE", unit="dona", is_active=False)
        response = self.patch(f"/api/vet/vaccinations/{vaccination_id}", {"product": other.pk})
        self.assertEqual(response.status_code, 400)
        self.assertIn("product", response.data)
        response = self.patch(f"/api/vet/vaccinations/{vaccination_id}", {"product": self.feed.pk})
        self.assertEqual(response.status_code, 400)

    def test_other_vet_cannot_complete_assigned_vaccination(self):
        vaccination_id = self.plan_vaccination().data["id"]
        self.login_as(self.vet2)
        response = self.post(f"/api/vet/vaccinations/{vaccination_id}/complete",
                             {"done_date": today().isoformat(), "birds_vaccinated": 5, "result": "OK"})
        self.assertEqual(response.status_code, 403)
        self.assertEqual(Vaccination.objects.get(pk=vaccination_id).status, VetStatus.PLANNED)

    def test_campaign_with_inactive_household_creates_nothing(self):
        self.household_b.is_active = False
        self.household_b.save()
        response = self.plan_vaccination(household=None, household_ids=[self.household_a.pk, self.household_b.pk])
        self.assertEqual(response.status_code, 400)
        self.assertIn("household_ids", response.data)
        self.assertFalse(Vaccination.objects.exists())
        self.assertFalse(Notification.objects.filter(event="vaccination_planned").exists())


class TreatmentStockTests(VetBase):
    def test_not_enough_stock_rolls_back_everything(self):
        self.stock_in(self.drug, 1)
        body = {"household": self.household_a.pk, "product": self.drug.pk, "start_date": today().isoformat(),
                "quantity_used": "2"}
        response = self.post("/api/vet/treatments", body)
        self.assertEqual(response.status_code, 400)
        self.assertIn("quantity_used", response.data)
        self.assertFalse(Treatment.objects.exists())
        self.assertFalse(StockTransaction.objects.filter(type="OUT").exists())
        self.assertFalse(AuditLog.objects.filter(object_type="Treatment").exists())
        self.assertFalse(Notification.objects.filter(event="treatment_prescribed").exists())
        self.drug.refresh_from_db()
        self.assertEqual(self.drug.balance, Decimal("1"))

    def test_treatment_without_product_moves_no_stock(self):
        response = self.post("/api/vet/treatments", {"household": self.household_a.pk, "drug_name": "Vitamin AD3",
                                                     "start_date": today().isoformat(), "quantity_used": "3"})
        self.assertEqual(response.status_code, 201, response.data)
        self.assertIsNone(response.data["stock_op_id"])
        self.assertFalse(StockTransaction.objects.exists())


class AuditTrailTests(VetBase):
    def test_every_change_is_audited(self):
        visit_id = self.plan_visit().data["id"]
        self.post(f"/api/vet/visits/{visit_id}/complete", {"health_status": "GOOD", "conclusion": "Sog‘lom"})
        vaccination_id = self.plan_vaccination().data["id"]
        self.post(f"/api/vet/vaccinations/{vaccination_id}/cancel", {"reason": "Vaksina kelmadi"})
        disease = self.post("/api/vet/diseases", {"household": self.household_a.pk, "disease_name": "Gumboro",
                                                  "diagnosed_date": today().isoformat(), "cases": 3}).data
        self.patch(f"/api/vet/diseases/{disease['id']}", {"status": "TREATMENT"})
        self.login_as(self.owner_a)
        request_id = self.post("/api/vet/requests", {"text": "Tovuqlar kasal"}).data["id"]
        self.login_as(self.vet)
        self.post(f"/api/vet/requests/{request_id}/take")
        rows = set(AuditLog.objects.filter(module="vet").values_list("object_type", "action"))
        self.assertTrue({
            ("VetVisit", "create"), ("VetVisit", "status_change"),
            ("Vaccination", "create"), ("Vaccination", "status_change"),
            ("DiseaseRecord", "create"), ("DiseaseRecord", "status_change"),
            ("VetRequest", "create"), ("VetRequest", "status_change"),
        } <= rows, rows)


class BotTasksUnmigratedTests(VetBase):
    def test_vet_tasks_fall_back_to_empty_lists_before_migrate(self):
        from django.contrib.auth import get_user_model

        get_user_model().objects.filter(pk=self.vet.pk).update(telegram_id=8802)
        self.client.force_authenticate(None)
        with self.settings(BOT_API_TOKEN=BOT_API_TOKEN), mock.patch("vet.selectors.tables_ready", return_value=False):
            response = self.client.get("/api/bot/users/8802/tasks", HTTP_X_BOT_TOKEN=BOT_API_TOKEN)
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["visits_today"], [])
        self.assertEqual(response.data["new_requests"], 0)


class VetTakeoverTests(VetBase):
    """A VET cannot reassign a colleague's visit/vaccination with PATCH and then complete it (§8.1)."""

    def test_vet_cannot_take_over_visit_of_another_vet(self):
        self.login_as(self.admin)
        visit_id = self.plan_visit(vet=self.vet.pk).data["id"]
        self.login_as(self.vet2)
        for body in ({"vet": self.vet2.pk}, {"vet": None}, {"purpose": "Boshqa"}):
            response = self.patch(f"/api/vet/visits/{visit_id}", body)
            self.assertEqual(response.status_code, 403, body)
        self.assertEqual(VetVisit.objects.get(pk=visit_id).vet_id, self.vet.pk)
        response = self.post(f"/api/vet/visits/{visit_id}/complete", {"health_status": "GOOD", "conclusion": "Yaxshi"})
        self.assertEqual(response.status_code, 403)

    def test_vet_may_take_an_unassigned_visit_but_not_give_it_away(self):
        self.login_as(self.admin)
        visit_id = self.plan_visit().data["id"]
        self.login_as(self.vet2)
        self.assertEqual(self.patch(f"/api/vet/visits/{visit_id}", {"vet": self.vet.pk}).status_code, 403)
        response = self.patch(f"/api/vet/visits/{visit_id}", {"vet": self.vet2.pk})
        self.assertEqual(response.status_code, 200, response.data)
        # Own row: re-sending the unchanged vet with other fields is fine; clearing it is not.
        self.assertEqual(self.patch(f"/api/vet/visits/{visit_id}", {"vet": self.vet2.pk, "purpose": "Qayta ko‘rik"}).status_code,
                         200)
        response = self.patch(f"/api/vet/visits/{visit_id}", {"vet": None})
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.data["detail"], "Boshqa veterinarga faqat administrator biriktira oladi.")
        # ADMIN still reassigns freely.
        self.login_as(self.admin)
        self.assertEqual(self.patch(f"/api/vet/visits/{visit_id}", {"vet": self.vet.pk}).status_code, 200)

    def test_vet_cannot_take_over_vaccination(self):
        vaccination_id = self.plan_vaccination().data["id"]  # assigned to vet1 (creator)
        self.login_as(self.vet2)
        response = self.patch(f"/api/vet/vaccinations/{vaccination_id}", {"vet": self.vet2.pk})
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.data["detail"], "Bu emlash boshqa veterinarga biriktirilgan.")
        self.assertEqual(Vaccination.objects.get(pk=vaccination_id).vet_id, self.vet.pk)

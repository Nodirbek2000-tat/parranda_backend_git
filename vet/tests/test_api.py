"""Veterinary module (API_CONTRACT_V3 §8, §9.4, §10, §14.2, §16.1, §17.3, §19.1 “Vet”)."""

from __future__ import annotations

import shutil
import tempfile
from datetime import timedelta
from decimal import Decimal

from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import connection
from django.test import override_settings
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from accounts.models import User
from audit.models import AuditLog
from common.roles import Role
from common.testing import BOT_API_TOKEN, BaseAPITestCase, make_household, make_user
from core.models import AttachmentPurpose, SystemSettings
from farm.models import MovementType, PoultryMovement
from notifications.models import Notification
from warehouse.models import Product, ProductCategory, StockTransaction, Unit

from .. import reminders
from ..models import DiseaseRecord, DiseaseStatus, RequestStatus, Vaccination, VetRequest, VetStatus, VetVisit

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64


def today():
    return timezone.localdate()


class VetBase(BaseAPITestCase):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.vet = make_user("vet1", Role.VET, first_name="Dilnoza", last_name="Karimova")
        cls.vet2 = make_user("vet2", Role.VET, first_name="Akmal")
        cls.logistics = make_user("logist1", Role.LOGISTICS)
        cls.vaccine = Product.objects.create(
            name="Nyukasl vaksinasi", category=ProductCategory.VACCINE, unit=Unit.PIECE, min_stock=Decimal("2")
        )
        cls.drug = Product.objects.create(
            name="Enrofloksatsin", category=ProductCategory.VET_DRUG, unit=Unit.LITRE, min_stock=Decimal("0")
        )
        cls.feed = Product.objects.create(name="Yem PK-1", category=ProductCategory.FEED, unit=Unit.KG)

    def setUp(self):
        super().setUp()
        self.login_as(self.vet)

    def stock_in(self, product, quantity):
        self.login_as(self.admin)
        response = self.client.post(
            "/api/stock-transactions",
            {"product": product.pk, "type": "IN", "quantity": str(quantity), "document_no": "K-1"},
            format="json",
        )
        self.assertEqual(response.status_code, 201, response.data)
        self.login_as(self.vet)

    def plan_visit(self, **extra):
        body = {"household": self.household_a.pk, "planned_date": today().isoformat(), "purpose": "Rejali ko‘rik"}
        body.update(extra)
        with self.captureOnCommitCallbacks(execute=True):
            response = self.client.post("/api/vet/visits", body, format="json")
        return response

    def plan_vaccination(self, **extra):
        body = {
            "household": self.household_a.pk,
            "planned_date": today().isoformat(),
            "product": self.vaccine.pk,
            "dose": "0.5 ml / bosh",
            "birds_planned": 300,
        }
        body.update(extra)
        with self.captureOnCommitCallbacks(execute=True):
            return self.client.post("/api/vet/vaccinations", body, format="json")

    def post(self, path, body=None):
        with self.captureOnCommitCallbacks(execute=True):
            return self.client.post(path, body or {}, format="json")

    def patch(self, path, body):
        with self.captureOnCommitCallbacks(execute=True):
            return self.client.patch(path, body, format="json")


class AccessMatrixTests(VetBase):
    """§1.2 row ``/api/vet/*`` for the 5 roles; owner isolation = 404."""

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.visit_b = VetVisit.objects.create(
            household=cls.household_b, planned_date=today(), purpose="B ko‘rik", created_by=cls.admin
        )
        cls.vaccination_a = Vaccination.objects.create(
            household=cls.household_a, batch=cls.batch, vaccine_name="ND", planned_date=today(), created_by=cls.admin
        )
        cls.request_b = VetRequest.objects.create(household=cls.household_b, created_by=cls.owner_b, text="Kasal tovuq")

    def test_lists_per_role(self):
        paths = ["/api/vet/visits", "/api/vet/vaccinations", "/api/vet/diseases", "/api/vet/treatments",
                 "/api/vet/requests"]
        expected = {self.admin: 200, self.vet: 200, self.owner_a: 200, self.warehouse: 403, self.logistics: 403}
        for user, code in expected.items():
            self.login_as(user)
            for path in paths:
                self.assertEqual(self.client.get(path).status_code, code, (user.username, path))
        for user, code in {self.admin: 200, self.vet: 200, self.owner_a: 403, self.warehouse: 403,
                           self.logistics: 403}.items():
            self.login_as(user)
            for path in ("/api/vet/summary", "/api/vet/calendar"):
                self.assertEqual(self.client.get(path).status_code, code, (user.username, path))

    def test_warehouse_reads_batch_vaccination_history_only(self):
        self.login_as(self.warehouse)
        response = self.client.get(f"/api/vet/vaccinations?batch={self.batch.pk}")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["count"], 1)
        self.assertEqual(self.client.get(f"/api/vet/vaccinations/{self.vaccination_a.pk}").status_code, 403)
        self.assertEqual(self.post("/api/vet/vaccinations", {}).status_code, 403)

    def test_owner_isolation(self):
        self.login_as(self.owner_a)
        self.assertEqual(self.client.get(f"/api/vet/visits/{self.visit_b.pk}").status_code, 404)
        self.assertEqual(self.client.get(f"/api/vet/requests/{self.request_b.pk}").status_code, 404)
        self.assertEqual(self.post(f"/api/vet/requests/{self.request_b.pk}/close").status_code, 404)
        self.assertEqual(self.client.get("/api/vet/visits").data["count"], 0)
        self.assertEqual(self.client.get(f"/api/vet/vaccinations/{self.vaccination_a.pk}").status_code, 200)
        self.login_as(self.owner_b)
        self.assertEqual(self.client.get(f"/api/vet/vaccinations/{self.vaccination_a.pk}").status_code, 404)

    def test_owner_cannot_write_staff_objects(self):
        self.login_as(self.owner_a)
        self.assertEqual(self.plan_visit().status_code, 403)
        self.assertEqual(self.post(f"/api/vet/visits/{self.visit_b.pk}/cancel", {"reason": "abc"}).status_code, 403)
        self.login_as(self.admin)
        self.assertEqual(self.post(f"/api/vet/requests/{self.request_b.pk}/take").status_code, 403)
        self.assertEqual(self.post("/api/vet/requests", {"text": "Salom salom"}).status_code, 403)

    def test_vet_reads_phase1_data_and_records_deaths_only(self):
        for path in ("/api/households", f"/api/households/{self.household_a.pk}", "/api/households/map",
                     f"/api/households/{self.household_a.pk}/batches", "/api/batches",
                     f"/api/batches/{self.batch.pk}", "/api/movements", "/api/daily-reports", "/api/products"):
            self.assertEqual(self.client.get(path).status_code, 200, path)
        self.assertEqual(self.client.post("/api/households", {}, format="json").status_code, 403)
        self.assertEqual(self.client.patch(f"/api/batches/{self.batch.pk}", {"supplier": "X"}, format="json")
                         .status_code, 403)
        self.assertEqual(self.client.post("/api/products", {}, format="json").status_code, 403)
        body = {"batch": self.batch.pk, "household": self.household_a.pk, "type": "DISTRIBUTE", "quantity": 1}
        response = self.client.post("/api/movements", body, format="json")
        self.assertEqual(response.status_code, 400)
        self.assertIn("type", response.data)
        response = self.client.post("/api/movements", {**body, "type": "DEATH", "household": None}, format="json")
        self.assertEqual(response.status_code, 400)
        self.assertIn("household", response.data)
        with self.captureOnCommitCallbacks(execute=True):
            response = self.client.post("/api/movements", {**body, "type": "DEATH", "note": "VT-000001"},
                                        format="json")
        self.assertEqual(response.status_code, 201, response.data)
        self.assertTrue(Notification.objects.filter(event="poultry_death", recipient=self.vet).exists())

    def test_logistics_never_sees_farm_data(self):
        self.login_as(self.logistics)
        for path in ("/api/households", "/api/batches", "/api/movements", "/api/daily-reports"):
            self.assertEqual(self.client.get(path).status_code, 403, path)


class VisitTests(VetBase):
    def test_plan_defaults_vet_to_self_and_notifies(self):
        response = self.plan_visit(batch=self.batch.pk)
        self.assertEqual(response.status_code, 201, response.data)
        data = response.data
        self.assertEqual(data["code"], f"VT-{data['id']:06d}")
        self.assertEqual(data["vet"]["id"], self.vet.pk)
        self.assertEqual(data["status"], "PLANNED")
        self.assertEqual(data["allowed_actions"], ["complete", "update", "cancel"])
        recipients = set(Notification.objects.filter(event="vet_visit_planned").values_list("recipient_id", flat=True))
        self.assertEqual(recipients, {self.vet.pk, self.owner_a.pk})
        owner_row = Notification.objects.get(event="vet_visit_planned", recipient=self.owner_a)
        self.assertEqual(owner_row.button_path, "/my/vet")
        self.assertIn(data["code"], owner_row.text)
        log = AuditLog.objects.get(object_type="VetVisit", action="create")
        self.assertEqual((log.module, log.op_id), ("vet", data["code"]))

    def test_admin_plan_without_vet_notifies_all_vets(self):
        self.login_as(self.admin)
        response = self.plan_visit()
        self.assertEqual(response.status_code, 201)
        self.assertIsNone(response.data["vet"])
        self.assertEqual(response.data["allowed_actions"], ["update", "cancel"])
        recipients = set(Notification.objects.filter(event="vet_visit_planned").values_list("recipient_id", flat=True))
        self.assertEqual(recipients, {self.vet.pk, self.vet2.pk, self.owner_a.pk})

    def test_plan_validation(self):
        cases = [
            ({"planned_date": (today() - timedelta(days=31)).isoformat()}, "planned_date"),
            ({"planned_date": (today() + timedelta(days=366)).isoformat()}, "planned_date"),
            ({"vet": self.admin.pk}, "vet"),
            ({"purpose": "ab"}, "purpose"),
        ]
        for extra, field in cases:
            response = self.plan_visit(**extra)
            self.assertEqual(response.status_code, 400, extra)
            self.assertIn(field, response.data)
        other = make_household("Yangi uy", "owner_c")
        response = self.plan_visit(household=other.pk, batch=self.batch.pk)
        self.assertEqual(response.data["batch"], ["Bu partiya ushbu xonadonga tarqatilmagan."])
        other.is_active = False
        other.save()
        response = self.plan_visit(household=other.pk)
        self.assertEqual(response.data["household"], ["Xonadon faol emas"])
        User.objects.filter(pk=self.vet2.pk).update(is_active=False)
        response = self.plan_visit(vet=self.vet2.pk)
        self.assertEqual(response.data["vet"], ["Faqat faol veterinarni tanlash mumkin."])
        self.assertFalse(VetVisit.objects.exists())
        self.assertFalse(AuditLog.objects.filter(module="vet").exists())

    def test_patch_date_refires_with_new_key(self):
        visit_id = self.plan_visit().data["id"]
        before = Notification.objects.filter(event="vet_visit_planned").count()
        response = self.patch(f"/api/vet/visits/{visit_id}", {"purpose": "Yangi maqsad"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(Notification.objects.filter(event="vet_visit_planned").count(), before)
        new_date = (today() + timedelta(days=3)).isoformat()
        response = self.patch(f"/api/vet/visits/{visit_id}", {"planned_date": new_date})
        self.assertEqual(response.data["planned_date"], new_date)
        self.assertEqual(Notification.objects.filter(event="vet_visit_planned").count(), before * 2)
        self.assertEqual(self.patch(f"/api/vet/visits/{visit_id}", {"status": "DONE"}).status_code, 400)
        self.assertEqual(AuditLog.objects.filter(object_type="VetVisit", action="update").count(), 2)

    def test_complete_rules_and_conclusion_notification(self):
        visit_id = self.plan_visit(batch=self.batch.pk).data["id"]
        body = {"health_status": "POOR", "birds_checked": 290, "findings": "Yo‘tal", "conclusion": "Nafas yo‘li",
                "recommendations": "Vitamin bering", "update_batch_health": True}
        self.login_as(self.vet2)
        response = self.post(f"/api/vet/visits/{visit_id}/complete", body)
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.data["detail"], "Bu tekshiruv boshqa veterinarga biriktirilgan.")
        self.login_as(self.admin)
        self.assertEqual(self.post(f"/api/vet/visits/{visit_id}/complete", body).status_code, 403)
        self.login_as(self.vet)
        response = self.post(f"/api/vet/visits/{visit_id}/complete", {**body, "conclusion": ""})
        self.assertEqual(response.status_code, 400)
        future = (timezone.now() + timedelta(hours=2)).isoformat()
        self.assertEqual(self.post(f"/api/vet/visits/{visit_id}/complete", {**body, "done_at": future})
                         .status_code, 400)
        response = self.post(f"/api/vet/visits/{visit_id}/complete", body)
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual((response.data["status"], response.data["health_status_display"]), ("DONE", "Yomon"))
        self.assertEqual(response.data["allowed_actions"], [])
        self.batch.refresh_from_db()
        self.assertEqual(self.batch.health_status, "Yomon")
        self.assertTrue(AuditLog.objects.filter(module="batches", action="update").exists())
        recipients = set(Notification.objects.filter(event="vet_conclusion").values_list("recipient_id", flat=True))
        self.assertEqual(recipients, {self.admin.pk, self.owner_a.pk})
        self.assertEqual(self.post(f"/api/vet/visits/{visit_id}/complete", body).status_code, 409)
        self.assertEqual(self.post(f"/api/vet/visits/{visit_id}/cancel", {"reason": "kech"}).status_code, 409)
        self.assertEqual(self.patch(f"/api/vet/visits/{visit_id}", {"purpose": "Boshqa"}).status_code, 409)

    def test_unassigned_visit_is_taken_by_completing_vet(self):
        self.login_as(self.admin)
        visit_id = self.plan_visit().data["id"]
        self.login_as(self.vet2)
        response = self.post(f"/api/vet/visits/{visit_id}/complete", {"health_status": "GOOD", "conclusion": "Sog‘lom"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["vet"]["id"], self.vet2.pk)

    def test_cancel(self):
        visit_id = self.plan_visit().data["id"]
        self.assertEqual(self.post(f"/api/vet/visits/{visit_id}/cancel", {"reason": "x"}).status_code, 400)
        self.login_as(self.admin)
        response = self.post(f"/api/vet/visits/{visit_id}/cancel", {"reason": "Ob-havo yomon"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["cancel_reason"], "Ob-havo yomon")
        recipients = set(Notification.objects.filter(event="vet_visit_cancelled").values_list("recipient_id", flat=True))
        self.assertEqual(recipients, {self.vet.pk, self.owner_a.pk})
        log = AuditLog.objects.get(object_type="VetVisit", action="status_change")
        self.assertEqual(log.after["status"], "CANCELLED")

    def test_list_filters(self):
        self.plan_visit()
        self.plan_visit(planned_date=(today() - timedelta(days=2)).isoformat())
        self.login_as(self.admin)
        self.plan_visit(planned_date=(today() + timedelta(days=2)).isoformat(), vet=self.vet2.pk)
        self.assertEqual(self.client.get("/api/vet/visits?overdue=1").data["count"], 1)
        self.login_as(self.vet)
        self.assertEqual(self.client.get("/api/vet/visits?vet=me").data["count"], 2)
        self.assertEqual(self.client.get(f"/api/vet/visits?vet={self.vet2.pk}").data["count"], 1)
        rows = self.client.get("/api/vet/visits").data["results"]
        self.assertEqual([row["planned_date"] for row in rows], sorted(row["planned_date"] for row in rows))
        self.assertTrue(rows[0]["is_overdue"])
        self.assertEqual(self.client.get("/api/vet/visits?status=XX").status_code, 400)
        self.assertEqual(self.client.get("/api/vet/visits?vet=abc").status_code, 400)

    def test_list_query_budget_constant(self):
        for _ in range(2):
            self.plan_visit(batch=self.batch.pk)
        with CaptureQueriesContext(connection) as small:
            self.assertEqual(self.client.get("/api/vet/visits").status_code, 200)
        for _ in range(8):
            self.plan_visit(batch=self.batch.pk)
        with CaptureQueriesContext(connection) as big:
            self.assertEqual(self.client.get("/api/vet/visits").status_code, 200)
        self.assertLessEqual(len(big), 6)
        self.assertEqual(len(small), len(big))


class FileBindingTests(VetBase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls._media = tempfile.mkdtemp(prefix="nnp-vet-media-")
        cls._override = override_settings(MEDIA_ROOT=cls._media, FILE_STORAGE="local")
        cls._override.enable()

    @classmethod
    def tearDownClass(cls):
        cls._override.disable()
        shutil.rmtree(cls._media, ignore_errors=True)
        super().tearDownClass()

    def upload(self, purpose):
        response = self.client.post(
            "/api/files", {"file": SimpleUploadedFile("a.png", PNG), "purpose": purpose}, format="multipart"
        )
        self.assertEqual(response.status_code, 201, response.data)
        return response.data["id"]

    def test_visit_files_and_owner_request_photos(self):
        visit_id = self.plan_visit().data["id"]
        file_id = self.upload(AttachmentPurpose.VET_VISIT)
        response = self.post(f"/api/vet/visits/{visit_id}/complete",
                             {"health_status": "GOOD", "conclusion": "Sog‘lom", "file_ids": [file_id]})
        self.assertEqual(response.status_code, 200)
        self.assertEqual([f["id"] for f in response.data["files"]], [file_id])
        self.login_as(self.owner_a)
        self.assertEqual(self.client.get(f"/api/files/{file_id}").status_code, 200)
        photo = self.upload(AttachmentPurpose.VET_REQUEST)
        response = self.post("/api/vet/requests", {"text": "Tovuqlar yemayapti", "file_ids": [photo]})
        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(len(response.data["files"]), 1)
        self.login_as(self.vet)
        self.assertEqual(self.client.get(f"/api/files/{photo}").status_code, 200)
        self.login_as(self.owner_b)
        self.assertEqual(self.client.get(f"/api/files/{photo}").status_code, 404)
        self.login_as(self.owner_a)
        too_many = [self.upload(AttachmentPurpose.VET_REQUEST) for _ in range(6)]
        response = self.post("/api/vet/requests", {"text": "Yana bir murojaat", "file_ids": too_many})
        self.assertEqual(response.status_code, 400)
        self.assertIn("file_ids", response.data)


class VaccinationTests(VetBase):
    def test_single_and_campaign_create(self):
        response = self.plan_vaccination()
        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(response.data["vaccine_name"], "Nyukasl vaksinasi")
        self.assertEqual(response.data["code"], f"EM-{response.data['id']:06d}")
        recipients = set(Notification.objects.filter(event="vaccination_planned").values_list("recipient_id", flat=True))
        self.assertEqual(recipients, {self.vet.pk, self.owner_a.pk})
        response = self.plan_vaccination(household=None, household_ids=[self.household_a.pk, self.household_b.pk])
        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(len(response.data["created"]), 2)
        self.assertEqual(Vaccination.objects.count(), 3)
        self.assertEqual(AuditLog.objects.filter(object_type="Vaccination", action="create").count(), 3)
        response = self.plan_vaccination(household=None, household_ids=[self.household_a.pk], batch=self.batch.pk)
        self.assertEqual(response.status_code, 400)
        self.assertIn("batch", response.data)
        response = self.plan_vaccination(household=None, household_ids=[self.household_a.pk, 999999])
        self.assertEqual(response.status_code, 400)
        self.assertEqual(Vaccination.objects.count(), 3)

    def test_product_must_be_vaccine(self):
        response = self.plan_vaccination(product=self.feed.pk)
        self.assertEqual(response.data["product"], ["Faqat “Vaksina” toifasidagi mahsulot tanlanadi."])
        response = self.plan_vaccination(product=None)
        self.assertIn("vaccine_name", response.data)
        self.assertEqual(self.plan_vaccination(product=None, vaccine_name="Gumboro").status_code, 201)

    def test_complete_with_stock_out_exactly_once(self):
        self.stock_in(self.vaccine, 10)
        vaccination_id = self.plan_vaccination().data["id"]
        url = f"/api/vet/vaccinations/{vaccination_id}/complete"
        body = {"done_date": today().isoformat(), "birds_vaccinated": 295, "quantity_used": "11", "result": "Yaxshi"}
        response = self.post(url, body)
        self.assertEqual(response.status_code, 400)
        self.assertIn("quantity_used", response.data)
        self.assertEqual(Vaccination.objects.get().status, VetStatus.PLANNED)
        response = self.post(url, {**body, "quantity_used": "3"})
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["status"], "DONE")
        self.assertIsNotNone(response.data["stock_op_id"])
        self.vaccine.refresh_from_db()
        self.assertEqual(self.vaccine.balance, Decimal("7"))
        out = StockTransaction.objects.get(type="OUT")
        self.assertEqual((out.document_no, out.household_id), (response.data["code"], self.household_a.pk))
        self.assertEqual(out.reference_type, "vaccination")
        # stock_out goes to WAREHOUSE only (no owner row), vaccination_done to ADMIN + owner
        self.assertFalse(Notification.objects.filter(event="stock_out", recipient=self.owner_a).exists())
        recipients = set(Notification.objects.filter(event="vaccination_done").values_list("recipient_id", flat=True))
        self.assertEqual(recipients, {self.admin.pk, self.owner_a.pk})
        self.assertEqual(self.post(url, {**body, "quantity_used": "3"}).status_code, 409)
        self.assertEqual(StockTransaction.objects.filter(type="OUT").count(), 1)

    def test_complete_without_product_moves_no_stock(self):
        vaccination_id = self.plan_vaccination(product=None, vaccine_name="Gumboro").data["id"]
        response = self.post(f"/api/vet/vaccinations/{vaccination_id}/complete",
                             {"done_date": today().isoformat(), "birds_vaccinated": 10, "quantity_used": "5",
                              "result": "OK"})
        self.assertEqual(response.status_code, 200, response.data)
        self.assertFalse(StockTransaction.objects.exists())

    def test_complete_validation(self):
        vaccination_id = self.plan_vaccination().data["id"]
        url = f"/api/vet/vaccinations/{vaccination_id}/complete"
        for body, field in (
            ({"done_date": (today() + timedelta(days=1)).isoformat(), "birds_vaccinated": 1, "result": "ok"}, "done_date"),
            ({"done_date": (today() - timedelta(days=31)).isoformat(), "birds_vaccinated": 1, "result": "ok"}, "done_date"),
            ({"done_date": today().isoformat(), "birds_vaccinated": 0, "result": "ok"}, "birds_vaccinated"),
        ):
            response = self.post(url, body)
            self.assertEqual(response.status_code, 400)
            self.assertIn(field, response.data)

    def test_cancel_and_due_within(self):
        first = self.plan_vaccination().data["id"]
        self.plan_vaccination(planned_date=(today() + timedelta(days=10)).isoformat())
        self.assertEqual(self.client.get("/api/vet/vaccinations?due_within=3").data["count"], 1)
        response = self.post(f"/api/vet/vaccinations/{first}/cancel", {"reason": "Vaksina kelmadi"})
        self.assertEqual(response.status_code, 200)
        self.assertTrue(Notification.objects.filter(event="vaccination_cancelled", recipient=self.owner_a).exists())
        self.assertEqual(self.client.get("/api/vet/vaccinations?due_within=3").data["count"], 0)


class DiseaseTreatmentTests(VetBase):
    def create_disease(self, **extra):
        body = {"household": self.household_a.pk, "batch": self.batch.pk, "disease_name": "Nyukasl",
                "diagnosed_date": (today() - timedelta(days=2)).isoformat(), "cases": 12, "symptoms": "Yo‘tal"}
        body.update(extra)
        return self.post("/api/vet/diseases", body)

    def test_disease_lifecycle(self):
        response = self.create_disease()
        self.assertEqual(response.status_code, 201, response.data)
        disease_id = response.data["id"]
        self.assertEqual(response.data["code"], f"KS-{disease_id:06d}")
        self.assertEqual(response.data["vet"]["id"], self.vet.pk)
        recipients = set(Notification.objects.filter(event="disease_registered").values_list("recipient_id", flat=True))
        self.assertEqual(recipients, {self.admin.pk, self.owner_a.pk})
        PoultryMovement.objects.create(batch=self.batch, household=self.household_a, type=MovementType.DEATH,
                                       quantity=4, date=today() - timedelta(days=1), created_by=self.admin)
        PoultryMovement.objects.create(batch=self.batch, household=self.household_a, type=MovementType.DEATH,
                                       quantity=9, date=today() - timedelta(days=5), created_by=self.admin)
        detail = self.client.get(f"/api/vet/diseases/{disease_id}").data
        self.assertEqual(detail["deaths_since_diagnosis"], 4)
        self.assertEqual(detail["treatments"], [])
        response = self.patch(f"/api/vet/diseases/{disease_id}", {"status": "RESOLVED"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["resolved_date"], today().isoformat())
        self.assertEqual(self.patch(f"/api/vet/diseases/{disease_id}", {"notes": "x"}).status_code, 409)

    def test_disease_validation(self):
        self.assertIn("diagnosed_date", self.create_disease(diagnosed_date=(today() + timedelta(days=1)).isoformat())
                      .data)
        self.assertIn("cases", self.create_disease(cases=0).data)
        disease_id = self.create_disease().data["id"]
        response = self.patch(f"/api/vet/diseases/{disease_id}",
                              {"status": "RESOLVED", "resolved_date": (today() - timedelta(days=5)).isoformat()})
        self.assertEqual(response.status_code, 400)
        self.login_as(self.owner_a)
        self.assertEqual(self.patch(f"/api/vet/diseases/{disease_id}", {"notes": "x"}).status_code, 403)

    def test_treatment_with_stock_and_disease_status(self):
        self.stock_in(self.drug, 5)
        disease_id = self.create_disease().data["id"]
        body = {"household": self.household_a.pk, "disease": disease_id, "product": self.drug.pk,
                "dosage": "1 ml / litr suv", "start_date": today().isoformat(), "quantity_used": "1.5",
                "instructions": "5 kun ichirilsin"}
        self.login_as(self.admin)
        self.assertEqual(self.post("/api/vet/treatments", body).status_code, 403)
        self.login_as(self.vet)
        response = self.post("/api/vet/treatments", {**body, "household": self.household_b.pk})
        self.assertEqual(response.data["disease"], ["Kasallik yozuvi boshqa xonadonga tegishli."])
        response = self.post("/api/vet/treatments", body)
        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(response.data["drug_name"], "Enrofloksatsin")
        self.assertEqual(response.data["code"], f"DV-{response.data['id']:06d}")
        self.drug.refresh_from_db()
        self.assertEqual(self.drug.balance, Decimal("3.5"))
        self.assertEqual(DiseaseRecord.objects.get().status, DiseaseStatus.TREATMENT)
        self.assertTrue(Notification.objects.filter(event="treatment_prescribed", recipient=self.owner_a).exists())
        detail = self.client.get(f"/api/vet/diseases/{disease_id}").data
        self.assertEqual(len(detail["treatments"]), 1)
        treatment_id = response.data["id"]
        response = self.patch(f"/api/vet/treatments/{treatment_id}", {"end_date": (today() + timedelta(days=4)).isoformat()})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.patch(f"/api/vet/treatments/{treatment_id}", {"drug_name": "X"}).status_code, 400)
        self.assertEqual(self.patch(f"/api/vet/treatments/{treatment_id}",
                                    {"end_date": (today() - timedelta(days=1)).isoformat()}).status_code, 400)
        response = self.post("/api/vet/treatments", {**body, "product": self.feed.pk})
        self.assertIn("product", response.data)


class RequestFlowTests(VetBase):
    def create(self, user=None, text="Tovuqlar kasal ko‘rinadi"):
        self.login_as(user or self.owner_a)
        return self.post("/api/vet/requests", {"text": text})

    def test_owner_request_take_close(self):
        response = self.create()
        self.assertEqual(response.status_code, 201, response.data)
        request_id = response.data["id"]
        self.assertEqual(response.data["household"]["id"], self.household_a.pk)
        self.assertEqual(response.data["allowed_actions"], ["close"])
        recipients = set(Notification.objects.filter(event="vet_request_created").values_list("recipient_id", flat=True))
        self.assertEqual(recipients, {self.vet.pk, self.vet2.pk})
        self.login_as(self.vet)
        response = self.post(f"/api/vet/requests/{request_id}/take", {"response": "Ertaga boraman"})
        self.assertEqual(response.data["status"], "IN_PROGRESS")
        self.assertEqual(self.post(f"/api/vet/requests/{request_id}/take").status_code, 409)
        self.login_as(self.owner_a)
        self.assertEqual(self.post(f"/api/vet/requests/{request_id}/close").status_code, 409)
        self.login_as(self.vet)
        response = self.post(f"/api/vet/requests/{request_id}/close", {"response": "ok"})
        self.assertEqual(response.status_code, 400)
        response = self.post(f"/api/vet/requests/{request_id}/close", {"response": "Davolash buyurildi"})
        self.assertEqual((response.status_code, response.data["status"]), (200, "CLOSED"))
        statuses = set(Notification.objects.filter(event="vet_request_updated").values_list("event_key", flat=True))
        self.assertEqual(statuses, {f"vet_request_updated:{request_id}.IN_PROGRESS:{self.owner_a.pk}",
                                    f"vet_request_updated:{request_id}.CLOSED:{self.owner_a.pk}"})

    def test_owner_withdraws_new_request_without_notification(self):
        request_id = self.create().data["id"]
        response = self.post(f"/api/vet/requests/{request_id}/close")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["response"], "Egasi tomonidan bekor qilindi")
        self.assertFalse(Notification.objects.filter(event="vet_request_updated").exists())

    def test_open_request_limit(self):
        for _ in range(5):
            self.assertEqual(self.create().status_code, 201)
        response = self.create()
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.data["non_field_errors"], ["Sizda 5 ta ochiq murojaat bor. Avval javobni kuting."])
        self.assertEqual(self.create(text="abc").status_code, 400)

    def test_plan_visit_from_request(self):
        request_id = self.create().data["id"]
        self.login_as(self.vet)
        response = self.post(f"/api/vet/requests/{request_id}/plan-visit",
                             {"planned_date": today().isoformat(), "purpose": "Murojaat bo‘yicha ko‘rik"})
        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(response.data["request"]["id"], request_id)
        self.assertEqual(response.data["household"]["id"], self.household_a.pk)
        self.assertEqual(VetRequest.objects.get().status, RequestStatus.IN_PROGRESS)
        response = self.post(f"/api/vet/visits/{response.data['id']}/complete",
                             {"health_status": "FAIR", "conclusion": "Kuzatuv"})
        self.assertEqual(response.status_code, 200)


class CalendarSummaryTests(VetBase):
    def test_calendar_zero_filled_and_limits(self):
        self.plan_visit()
        self.plan_vaccination()
        cancelled = self.plan_vaccination().data["id"]
        self.post(f"/api/vet/vaccinations/{cancelled}/cancel", {"reason": "bekor"})
        date_from = today() - timedelta(days=1)
        date_to = today() + timedelta(days=1)
        with CaptureQueriesContext(connection) as ctx:
            response = self.client.get(f"/api/vet/calendar?date_from={date_from}&date_to={date_to}")
        self.assertEqual(response.status_code, 200)
        self.assertLessEqual(len(ctx), 3)
        self.assertEqual([day["date"] for day in response.data],
                         [d.isoformat() for d in (date_from, today(), date_to)])
        middle = response.data[1]
        self.assertEqual((len(middle["visits"]), len(middle["vaccinations"])), (1, 1))
        self.assertEqual(response.data[0]["visits"], [])
        too_long = today() + timedelta(days=70)
        response = self.client.get(f"/api/vet/calendar?date_from={today()}&date_to={too_long}")
        self.assertEqual(response.data["non_field_errors"], ["Davr 62 kundan oshmasligi kerak."])
        default = self.client.get("/api/vet/calendar").data
        self.assertEqual(default[0]["date"], today().replace(day=1).isoformat())

    def test_summary_and_my_vet(self):
        self.plan_visit()
        self.plan_visit(planned_date=(today() - timedelta(days=3)).isoformat())
        self.plan_vaccination(planned_date=(today() + timedelta(days=2)).isoformat())
        self.plan_vaccination(planned_date=(today() - timedelta(days=1)).isoformat())
        self.login_as(self.owner_a)
        self.post("/api/vet/requests", {"text": "Yordam kerak, iltimos"})
        self.login_as(self.vet)
        with CaptureQueriesContext(connection) as ctx:
            data = self.client.get("/api/vet/summary").data
        self.assertLessEqual(len(ctx), 10)
        self.assertEqual(data["visits"], {"today": 1, "planned_7d": 1, "overdue": 1, "done_30d": 0})
        self.assertEqual(data["vaccinations"], {"due_7d": 1, "overdue": 1, "done_30d": 0})
        self.assertEqual(data["requests"], {"new": 1, "in_progress": 0})
        self.assertTrue(data["today_visits"][0]["mine"])
        self.assertEqual(len(data["new_requests"]), 1)
        self.login_as(self.owner_a)
        data = self.client.get("/api/my/vet").data
        self.assertEqual(data["household"]["id"], self.household_a.pk)
        self.assertEqual(len(data["upcoming_visits"]), 2)
        self.assertEqual(len(data["upcoming_vaccinations"]), 2)
        self.assertEqual(len(data["requests"]), 1)
        self.login_as(self.owner_b)
        self.assertEqual(self.client.get("/api/my/vet").data["upcoming_visits"], [])

    def test_bot_summary_and_tasks_for_vet(self):
        User.objects.filter(pk=self.vet.pk).update(telegram_id=8801)
        self.plan_visit()
        self.plan_vaccination(planned_date=(today() + timedelta(days=1)).isoformat())
        self.client.force_authenticate(None)
        headers = {"HTTP_X_BOT_TOKEN": BOT_API_TOKEN}
        with self.settings(BOT_API_TOKEN=BOT_API_TOKEN):
            summary = self.client.get("/api/bot/users/8801/summary", **headers)
            tasks = self.client.get("/api/bot/users/8801/tasks", **headers)
            missing = self.client.get("/api/bot/users/9999/tasks", **headers)
        self.assertEqual(summary.status_code, 200, summary.data)
        self.assertEqual(summary.data["role"], "VET")
        self.assertEqual(summary.data["today"]["visits_today"], 1)
        self.assertEqual(tasks.status_code, 200)
        self.assertEqual(len(tasks.data["visits_today"]), 1)
        self.assertEqual(len(tasks.data["vaccinations_due"]), 1)
        self.assertEqual(missing.status_code, 404)

    def test_search_group(self):
        visit = self.plan_visit().data
        data = self.client.get(f"/api/search?q={visit['code']}").data
        group = next(g for g in data["groups"] if g["type"] == "vet_visit")
        self.assertEqual(group["items"][0]["path"], f"/vet/visits/{visit['id']}")
        self.login_as(self.owner_b)
        groups = self.client.get(f"/api/search?q={visit['code']}").data["groups"]
        self.assertFalse(any(g["type"] == "vet_visit" for g in groups))


class ReminderTests(VetBase):
    def test_stages_and_idempotency(self):
        SystemSettings.objects.filter(pk=1).update(vaccination_reminder_days=3)
        current = today()
        due = Vaccination.objects.create(household=self.household_a, vaccine_name="ND", vet=self.vet,
                                         planned_date=current + timedelta(days=2))
        on_day = Vaccination.objects.create(household=self.household_a, vaccine_name="ND", planned_date=current)
        late7 = Vaccination.objects.create(household=self.household_a, vaccine_name="ND", vet=self.vet,
                                           planned_date=current - timedelta(days=7))
        Vaccination.objects.create(household=self.household_a, vaccine_name="ND", vet=self.vet,
                                   planned_date=current - timedelta(days=3))  # not a stage day
        visit = VetVisit.objects.create(household=self.household_a, vet=self.vet, planned_date=current,
                                        purpose="Ko‘rik")
        queued = (reminders.vaccination_due_job(current) + reminders.vaccination_overdue_job(current)
                  + reminders.vet_visit_today_job(current))
        self.assertGreater(queued, 0)
        keys = set(Notification.objects.values_list("event_key", flat=True))
        self.assertIn(f"vaccination_due:{due.pk}.{due.planned_date:%Y%m%d}.due:{self.vet.pk}", keys)
        self.assertIn(f"vaccination_due:{due.pk}.{due.planned_date:%Y%m%d}.due:{self.owner_a.pk}", keys)
        # unassigned → all vets
        self.assertIn(f"vaccination_due:{on_day.pk}.{current:%Y%m%d}.today:{self.vet2.pk}", keys)
        self.assertIn(f"vaccination_overdue:{late7.pk}.{late7.planned_date:%Y%m%d}.od7:{self.vet.pk}", keys)
        self.assertIn(f"vet_visit_today:{visit.pk}.{current:%Y%m%d}:{self.vet.pk}", keys)
        self.assertFalse(Notification.objects.filter(event="vaccination_overdue", recipient=self.owner_a).exists())
        self.assertEqual(Notification.objects.filter(event="vaccination_overdue").count(), 2)  # vet + admin
        again = (reminders.vaccination_due_job(current) + reminders.vaccination_overdue_job(current)
                 + reminders.vet_visit_today_job(current))
        self.assertEqual(again, 0)

    def test_rescheduled_vaccination_is_reminded_again(self):
        SystemSettings.objects.filter(pk=1).update(vaccination_reminder_days=3)
        current = today()
        row = Vaccination.objects.create(household=self.household_a, vaccine_name="ND", vet=self.vet,
                                         planned_date=current - timedelta(days=1))
        self.assertGreater(reminders.vaccination_overdue_job(current), 0)  # od1 for the missed date
        new_date = current + timedelta(days=2)
        response = self.patch(f"/api/vet/vaccinations/{row.pk}", {"planned_date": new_date.isoformat()})
        self.assertEqual(response.status_code, 200, response.data)
        # The new plan is announced again (new key) ...
        self.assertEqual(Notification.objects.filter(event="vaccination_planned", recipient=self.owner_a).count(), 1)
        # ... and the reminders for the new date are not blocked by the old date's keys.
        self.assertGreater(reminders.vaccination_due_job(current), 0)
        self.assertGreater(reminders.vaccination_due_job(new_date), 0)  # ".today" for the new date
        self.assertGreater(reminders.vaccination_overdue_job(new_date + timedelta(days=1)), 0)
        self.assertEqual(reminders.vaccination_overdue_job(new_date + timedelta(days=1)), 0)  # idempotent

    def test_jobs_registered(self):
        from notifications import reminders as engine

        names = {job.name for job in engine.jobs()}
        self.assertTrue({"vaccination_due", "vaccination_overdue", "vet_visit_today"} <= names)


class IntegrityTests(VetBase):
    def test_vet_invariants(self):
        from .. import integrity

        self.stock_in(self.vaccine, 5)
        vaccination_id = self.plan_vaccination().data["id"]
        self.post(f"/api/vet/vaccinations/{vaccination_id}/complete",
                  {"done_date": today().isoformat(), "birds_vaccinated": 5, "quantity_used": "1", "result": "OK"})
        self.assertEqual(integrity.problems(), [])
        Vaccination.objects.filter(pk=vaccination_id).update(quantity_used=Decimal("2"))
        self.assertEqual(len(integrity.problems()), 1)
        Vaccination.objects.filter(pk=vaccination_id).update(stock_transaction=None)
        self.assertIn("ombor chiqimi yo‘q", integrity.problems()[0])
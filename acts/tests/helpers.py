"""Fixtures of the acts tests: real work of every role created through the domain services."""

from __future__ import annotations

import shutil
import tempfile
from datetime import timedelta
from decimal import Decimal

from django.core.files.base import ContentFile
from django.test import override_settings
from django.utils import timezone

from builder.models import Blueprint, BlueprintKind, BlueprintStatus, FormSubmission, TaskInstance, TaskStatus
from common.roles import Role
from common.testing import BaseAPITestCase, make_household, make_product, make_user
from core.models import Attachment, AttachmentPurpose
from core.settings_service import get_settings
from farm import services as farm_services
from farm.models import MovementType
from logistics import services as logistics_services
from logistics.models import Driver, Vehicle
from vet.models import DiseaseRecord, HealthStatus, RequestStatus, Vaccination, VetRequest, VetStatus, VetVisit
from warehouse import services as warehouse_services
from warehouse.models import StockTransactionType

def png_bytes() -> bytes:
    from io import BytesIO

    from PIL import Image

    buffer = BytesIO()
    Image.new("RGB", (40, 30), (40, 120, 80)).save(buffer, format="PNG")
    return buffer.getvalue()


def today():
    return timezone.localdate()


class ActsTestCase(BaseAPITestCase):
    """Base households A/B + one user of every staff role and one finished piece of work for each of them."""

    @classmethod
    def setUpClass(cls):
        # Before super(): setUpTestData (run by it) already writes the fixture photo.
        cls._media = tempfile.mkdtemp(prefix="nnp-acts-media-")
        cls._override = override_settings(MEDIA_ROOT=cls._media, FILE_STORAGE="local")
        cls._override.enable()
        super().setUpClass()

    @classmethod
    def tearDownClass(cls):
        cls._override.disable()
        shutil.rmtree(cls._media, ignore_errors=True)
        super().tearDownClass()

    @classmethod
    def setUpTestData(cls) -> None:
        super().setUpTestData()
        cls.vet = make_user("vet1", Role.VET, first_name="Dilnoza", last_name="Karimova")
        cls.vet2 = make_user("vet2", Role.VET, first_name="Akmal")
        cls.logist = make_user("logist1", Role.LOGISTICS, first_name="Sardor")
        cls.warehouse2 = make_user("ombor2", Role.WAREHOUSE)
        cls.household_c = make_household("Uzoq qishloq 1-uy", "owner_c")
        cls.household_c.district = "Pop tumani"
        cls.household_c.save(update_fields=["district"])
        settings = get_settings()
        settings.delivery_proof_required = False
        settings.save()

        # WAREHOUSE: receipt, issue to household A, poultry distribution to household A.
        cls.feed = make_product("Yem PK-1", min_stock="10")
        warehouse_services.record_stock_transaction(
            actor=cls.warehouse, product_id=cls.feed.pk, transaction_type=StockTransactionType.IN,
            quantity=Decimal("1000"), document_no="KR-1",
        )
        warehouse_services.record_stock_transaction(
            actor=cls.warehouse, product_id=cls.feed.pk, transaction_type=StockTransactionType.OUT,
            quantity=Decimal("40"), document_no="CH-1", household=cls.household_a,
        )
        farm_services.record_movement(
            actor=cls.warehouse, batch=cls.batch, household=cls.household_a,
            movement_type=MovementType.DISTRIBUTE, quantity=50, on_date=today(),
        )

        # LOGISTICS: one task delivered to household A, one cancelled (not counted).
        cls.vehicle = Vehicle.objects.create(plate_number="01 A 123 BC", model="Isuzu", capacity_kg=Decimal("3000"))
        cls.driver = Driver.objects.create(full_name="Sardor Rahimov", phone="+998901112233", user=cls.logist)
        item = logistics_services.ItemInput(product=cls.feed, quantity=Decimal("150"))
        task = logistics_services.create_task(
            actor=cls.admin, household=cls.household_a, items=[item], vehicle=cls.vehicle.pk, driver=cls.driver.pk,
        )
        logistics_services.accept(actor=cls.logist, task_id=task.pk)
        logistics_services.start(actor=cls.logist, task_id=task.pk)
        logistics_services.deliver(actor=cls.logist, task_id=task.pk, recipient_name="Bobur")
        cls.delivery = task
        cancelled = logistics_services.create_task(
            actor=cls.admin, household=cls.household_a, items=[item], vehicle=cls.vehicle.pk, driver=cls.driver.pk,
        )
        logistics_services.cancel_task(actor=cls.admin, task_id=cancelled.pk, reason="Kerak emas")

        # VET: done visit, done vaccination, disease, closed request (+ a cancelled visit, not counted).
        now = timezone.now()
        cls.visit = VetVisit.objects.create(
            household=cls.household_a, vet=cls.vet, planned_date=today(), purpose="Rejali ko‘rik",
            status=VetStatus.DONE, done_at=now, birds_checked=250, health_status=HealthStatus.GOOD,
            conclusion="Sog‘lom", created_by=cls.admin,
        )
        VetVisit.objects.create(
            household=cls.household_a, vet=cls.vet, planned_date=today(), purpose="Bekor",
            status=VetStatus.CANCELLED, cancel_reason="—", created_by=cls.admin,
        )
        VetVisit.objects.create(
            household=cls.household_b, vet=cls.vet2, planned_date=today(), purpose="Boshqa vet",
            status=VetStatus.DONE, done_at=now, birds_checked=100, health_status=HealthStatus.FAIR,
            conclusion="Qoniqarli", created_by=cls.admin,
        )
        Vaccination.objects.create(
            household=cls.household_a, vet=cls.vet, vaccine_name="Nyukasl", planned_date=today(),
            status=VetStatus.DONE, done_date=today(), birds_vaccinated=240, created_by=cls.admin,
        )
        DiseaseRecord.objects.create(
            household=cls.household_a, vet=cls.vet, disease_name="Kolibakterioz", diagnosed_date=today(), cases=3,
            created_by=cls.vet,
        )
        VetRequest.objects.create(
            household=cls.household_a, created_by=cls.owner_a, text="Parrandalar kasal", status=RequestStatus.CLOSED,
            vet=cls.vet, response="Ko‘rildi", closed_at=now, closed_by=cls.vet,
        )

        # OWNER: two daily reports of household A.
        for days_ago, meat in ((1, "20"), (0, "30")):
            farm_services.create_daily_report(
                actor=cls.admin, household=cls.household_a, on_date=today() - timedelta(days=days_ago),
                feed_kg=Decimal("15"), meat_kg=Decimal(meat), avg_weight_g=Decimal("1800"), deaths=1,
            )

        # Builder: an approved VET task with a form submission (number + photo), and an open one.
        png = png_bytes()
        cls.photo = Attachment.objects.create(
            file=ContentFile(png, name="rasm.png"), original_name="rasm.png", content_type="image/png",
            size=len(png), sha256="0" * 64, purpose=AttachmentPurpose.VET_VISIT, uploaded_by=cls.vet,
        )
        cls.form = Blueprint.objects.create(
            kind=BlueprintKind.FORM, name="Ko‘rik formasi", status=BlueprintStatus.ACTIVE,
            data={"roles": ["VET"], "fields": [
                {"key": "area_m2", "label": "Maydon (m²)", "type": "number"},
                {"key": "photo", "label": "Rasm", "type": "photo"},
            ]},
        )
        cls.template = Blueprint.objects.create(
            kind=BlueprintKind.TASK, name="Ko‘rik vazifasi", status=BlueprintStatus.ACTIVE,
            data={"executor": "VET", "approver": "ADMIN", "form_id": cls.form.pk, "document_id": None},
        )
        cls.task = cls.make_task(cls.household_a, status=TaskStatus.APPROVED, values={"area_m2": 120,
                                                                                       "photo": cls.photo.pk})
        cls.open_task = cls.make_task(cls.household_a, status=TaskStatus.SUBMITTED, values={"area_m2": 5})

    @classmethod
    def make_task(cls, household, *, status, values, executor="VET", is_test=False, definition=None):
        now = timezone.now()
        approved = status == TaskStatus.APPROVED
        task = TaskInstance.objects.create(
            template=cls.template, template_version=1, title="Xonadon ko‘rigi", executor=executor,
            approver="ADMIN", household=household, definition=definition or dict(cls.template.data),
            status=status, submitted_by=cls.vet, submitted_at=now, is_test=is_test,
            decided_by=cls.admin if approved else None, decided_at=now if approved else None, created_by=cls.admin,
        )
        FormSubmission.objects.create(
            form=cls.form, form_version=1, fields=cls.form.data["fields"], data=values, household=household,
            task=task, submitted_by=cls.vet, is_test=is_test,
        )
        return task

    # --- helpers -----------------------------------------------------------------------------------

    def period(self) -> dict:
        return {"date_from": (today() - timedelta(days=10)).isoformat(), "date_to": today().isoformat()}

    def preview(self, user, **params):
        self.login_as(user)
        return self.client.get("/api/acts/preview", {**self.period(), **params})

    def create(self, user, **body):
        self.login_as(user)
        return self.client.post("/api/acts", {**self.period(), **body}, format="json")

    @staticmethod
    def section(snapshot: dict, title: str) -> dict:
        return next(section for section in snapshot["sections"] if section["title"] == title)

    @staticmethod
    def summary(snapshot: dict) -> dict[str, str]:
        return {item["label"]: item["value"] for item in snapshot["summary"]}

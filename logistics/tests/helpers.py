"""Fixtures of the logistics tests."""

from __future__ import annotations

import shutil
import tempfile
from decimal import Decimal

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import override_settings

from common.roles import Role
from common.testing import BaseAPITestCase, make_product, make_user
from core.models import AttachmentPurpose
from logistics.models import Driver, Vehicle
from warehouse import services as warehouse_services
from warehouse.models import StockTransactionType, Unit

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64


def stock_in(actor, product, quantity: str) -> None:
    warehouse_services.record_stock_transaction(
        actor=actor,
        product_id=product.pk,
        transaction_type=StockTransactionType.IN,
        quantity=Decimal(quantity),
        document_no="KR-1",
    )


class LogisticsTestCase(BaseAPITestCase):
    """Base fixtures + two logistics workers, a vehicle, a driver (linked to ``logist``) and stocked products."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls._media = tempfile.mkdtemp(prefix="nnp-logistics-media-")
        cls._override = override_settings(MEDIA_ROOT=cls._media, FILE_STORAGE="local")
        cls._override.enable()

    @classmethod
    def tearDownClass(cls):
        cls._override.disable()
        shutil.rmtree(cls._media, ignore_errors=True)
        super().tearDownClass()

    @classmethod
    def setUpTestData(cls) -> None:
        super().setUpTestData()
        cls.logist = make_user("logist1", Role.LOGISTICS, telegram_id=7001)
        cls.logist2 = make_user("logist2", Role.LOGISTICS, telegram_id=7002)
        cls.vet = make_user("vet1", Role.VET)
        cls.feed = make_product("Yem PK-1", min_stock="10")
        cls.vitamin = make_product("Vitamin", min_stock="0", unit=Unit.PIECE)
        stock_in(cls.admin, cls.feed, "1000")
        stock_in(cls.admin, cls.vitamin, "50")
        cls.vehicle = Vehicle.objects.create(plate_number="01 A 123 BC", model="Isuzu NPR", capacity_kg=Decimal("3000"))
        cls.small_vehicle = Vehicle.objects.create(plate_number="01 B 777 CC", model="Damas", capacity_kg=Decimal("100"))
        cls.driver = Driver.objects.create(full_name="Sardor Rahimov", phone="+998901112233", user=cls.logist)
        cls.driver2 = Driver.objects.create(full_name="Akmal Tursunov", phone="+998901112244")

    # --- helpers ------------------------------------------------------------------------------

    def create_task(self, quantity: int = 150, **extra):
        self.login_as(self.admin)
        body = {
            "household": self.household_a.pk,
            "items": [{"product": self.feed.pk, "quantity": quantity}],
            **extra,
        }
        response = self.client.post("/api/logistics/tasks", body, format="json")
        self.assertEqual(response.status_code, 201, response.data)
        return response.data

    def act(self, user, task_id: int, action: str, body=None):
        self.login_as(user)
        return self.client.post(f"/api/logistics/tasks/{task_id}/{action}", body or {}, format="json")

    def upload_proof(self, user) -> int:
        self.login_as(user)
        response = self.client.post(
            "/api/files",
            {"file": SimpleUploadedFile("dalil.png", PNG), "purpose": AttachmentPurpose.DELIVERY_PROOF},
            format="multipart",
        )
        self.assertEqual(response.status_code, 201, response.data)
        return response.data["id"]

    def to_status(self, task_id: int, target: str) -> None:
        """Drive a task forward with the happy-path actors."""
        steps = [
            ("ASSIGNED", self.admin, "assign", {"vehicle": self.vehicle.pk, "driver": self.driver.pk}),
            ("ACCEPTED", self.logist, "accept", {}),
            ("IN_TRANSIT", self.logist, "start", {}),
        ]
        for status, user, action, body in steps:
            response = self.act(user, task_id, action, body)
            self.assertEqual(response.status_code, 200, response.data)
            if status == target:
                return
        file_id = self.upload_proof(self.logist)
        response = self.act(self.logist, task_id, "deliver", {"file_ids": [file_id], "recipient_name": "Bobur"})
        self.assertEqual(response.status_code, 200, response.data)
        if target == "DELIVERED":
            return
        response = self.act(self.owner_a, task_id, "confirm", {"received_ok": True})
        self.assertEqual(response.status_code, 200, response.data)

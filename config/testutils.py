"""Shared helpers for the Django TestCase suites."""

import io
import shutil
import tempfile

from django.test import TestCase, override_settings
from PIL import Image
from rest_framework.test import APIClient

from accounts.models import Role, User
from stock.models import Package, PackageItem, Product, StockMove

_MEDIA = tempfile.mkdtemp(prefix="nnp-test-media-")


def png_bytes() -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (8, 8), (20, 120, 60)).save(buf, "PNG")
    return buf.getvalue()


@override_settings(MEDIA_ROOT=_MEDIA, BOT_API_TOKEN="bot-secret", BOT_TOKEN="123:test-bot-token")
class NNPTestCase(TestCase):
    @classmethod
    def tearDownClass(cls):
        super().tearDownClass()
        shutil.rmtree(_MEDIA, ignore_errors=True)

    @classmethod
    def setUpTestData(cls):
        cls.users = {}
        for role in Role.values:
            cls.users[role] = User.objects.create_user(username=role.lower(), password="Parol-12345", role=role,
                                                       full_name=f"{role} user")
        cls.product_a = Product.objects.create(name="Sement", unit="qop", category="material", balance=100)
        cls.product_b = Product.objects.create(name="Yem", unit="qop", category="yem", balance=10)
        for product in (cls.product_a, cls.product_b):
            StockMove.objects.create(product=product, type="IN", qty=product.balance, balance_after=product.balance)
        cls.package = Package.objects.create(name="1 sotix", area_sotix=1, capacity_birds=100, price=1000)
        PackageItem.objects.create(package=cls.package, product=cls.product_a, qty=10)
        PackageItem.objects.create(package=cls.package, product=cls.product_b, qty=4)

    def client_for(self, role_or_user) -> APIClient:
        user = self.users[role_or_user] if isinstance(role_or_user, str) else role_or_user
        client = APIClient()
        client.force_authenticate(user)
        return client

    def upload_photo(self, role_or_user, live=True) -> int:
        from django.core.files.uploadedfile import SimpleUploadedFile

        image = SimpleUploadedFile("p.png", png_bytes(), content_type="image/png")
        response = self.client_for(role_or_user).post(
            "/api/files/photo", {"image": image, "live": "true" if live else "false", "lat": "41.311081",
                                 "lng": "69.240562"}, format="multipart")
        self.assertEqual(response.status_code, 201, response.content)
        return response.json()["id"]

    def create_case(self, role=Role.INSPECTOR, **extra) -> dict:
        body = {
            "name": "Karimovlar xonadoni", "owner_full_name": "Karimov Anvar", "owner_phone": "+998901234567",
            "region": "Namangan", "district": "Pop", "mfy": "Navobod", "address": "1-uy",
            "latitude": 40.99887766554433, "longitude": 71.12345678901234, "package_id": self.package.pk,
            "conclusion": "Xonadon talabga javob beradi", "photo_ids": [self.upload_photo(role)], **extra,
        }
        with self.captureOnCommitCallbacks(execute=True):
            response = self.client_for(role).post("/api/cases", body, format="json")
        self.assertEqual(response.status_code, 201, response.content)
        return response.json()

    def act(self, role_or_user, case_id, n, action, body=None, expect=200):
        with self.captureOnCommitCallbacks(execute=True):
            response = self.client_for(role_or_user).post(
                f"/api/cases/{case_id}/stages/{n}/{action}", body or {}, format="json")
        self.assertEqual(response.status_code, expect, response.content)
        return response.json()

    def submit(self, role_or_user, case_id, n, data=None, photo=False, expect=200, note=""):
        body = {"data": data or {}, "note": note}
        if photo:
            body["photo_ids"] = [self.upload_photo(role_or_user)]
        return self.act(role_or_user, case_id, n, "submit", body, expect)

    def stage(self, detail, n) -> dict:
        return detail["stages"][n - 1]

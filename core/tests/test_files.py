"""Private files (API_CONTRACT_V3 §4): validation, purposes, binding, visibility, headers, cleanup."""

from __future__ import annotations

import io
import shutil
import tempfile
from datetime import timedelta
from pathlib import Path

from django.core.files.uploadedfile import SimpleUploadedFile
from django.core.management import call_command
from django.db import transaction
from django.test import override_settings
from django.utils import timezone
from rest_framework.exceptions import ValidationError

from audit.models import AuditLog
from common.roles import Role
from common.testing import BaseAPITestCase, make_user
from core import files
from core.models import Attachment, AttachmentPurpose

JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 64
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64
WEBP = b"RIFF\x00\x00\x00\x00WEBPVP8 " + b"\x00" * 64
PDF = b"%PDF-1.7\n" + b"\x00" * 64


class FileTestMixin:
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls._media = tempfile.mkdtemp(prefix="nnp-media-")
        cls._override = override_settings(MEDIA_ROOT=cls._media, FILE_STORAGE="local")
        cls._override.enable()

    @classmethod
    def tearDownClass(cls):
        cls._override.disable()
        shutil.rmtree(cls._media, ignore_errors=True)
        super().tearDownClass()

    def upload(self, name: str, content: bytes, purpose: str = AttachmentPurpose.DELIVERY_PROOF):
        return self.client.post(
            "/api/files", {"file": SimpleUploadedFile(name, content), "purpose": purpose}, format="multipart"
        )


class UploadTests(FileTestMixin, BaseAPITestCase):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.logistics = make_user("logist1", Role.LOGISTICS)
        cls.vet = make_user("vet1", Role.VET)

    def test_upload_returns_file_object_and_audits(self):
        self.login_as(self.logistics)
        response = self.upload("dalil rasmi.jpg", JPEG)
        self.assertEqual(response.status_code, 201, response.data)
        attachment = Attachment.objects.get()
        self.assertEqual(
            response.data,
            {
                "id": attachment.pk,
                "name": "dalil rasmi.jpg",
                "content_type": "image/jpeg",
                "size": len(JPEG),
                "purpose": "DELIVERY_PROOF",
                "url": f"/api/files/{attachment.pk}",
                "created_at": response.data["created_at"],
            },
        )
        self.assertRegex(attachment.file.name, r"^uploads/\d{4}/\d{2}/[0-9a-f]{32}\.jpg$")
        self.assertTrue(Path(self._media, attachment.file.name).exists())
        log = AuditLog.objects.get(module="files", action="upload")
        self.assertEqual(log.object_type, "Attachment")
        self.assertNotIn(attachment.file.name, str(log.after))

    def test_every_allowed_type(self):
        self.login_as(self.vet)
        for name, content, ctype in (("a.png", PNG, "image/png"), ("b.webp", WEBP, "image/webp"),
                                     ("c.PDF", PDF, "application/pdf"), ("d.jpeg", JPEG, "image/jpeg")):
            response = self.upload(name, content, AttachmentPurpose.VET_VISIT)
            self.assertEqual(response.status_code, 201, name)
            self.assertEqual(response.data["content_type"], ctype)

    def test_validation_order_and_messages(self):
        self.login_as(self.admin)
        response = self.client.post("/api/files", {"purpose": "DELIVERY_PROOF"}, format="multipart")
        self.assertEqual(response.data, {"file": ["Fayl tanlanmagan."]})
        big = SimpleUploadedFile("big.jpg", JPEG + b"\x00" * (files.MAX_UPLOAD_BYTES))
        response = self.client.post("/api/files", {"file": big, "purpose": "DELIVERY_PROOF"}, format="multipart")
        self.assertEqual(response.data, {"file": ["Fayl hajmi 10 MB dan oshmasligi kerak."]})
        bad_type = ["Faqat JPG, PNG, WEBP yoki PDF fayl yuklash mumkin."]
        self.assertEqual(self.upload("fake.jpg", PDF).data, {"file": bad_type})  # extension/magic mismatch
        self.assertEqual(self.upload("page.png", b"<html><script>x</script></html>").data, {"file": bad_type})
        self.assertEqual(self.upload("script.exe", JPEG).data, {"file": bad_type})
        self.assertEqual(self.upload("noext", JPEG).data, {"file": bad_type})
        response = self.upload("a.jpg", JPEG, "SELFIE")
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.data, {"purpose": ["“SELFIE” ruxsat etilgan qiymat emas."]})
        self.assertEqual(Attachment.objects.count(), 0)

    def test_purpose_per_role(self):
        matrix = [
            (self.warehouse, "DELIVERY_PROOF", 403),
            (self.logistics, "VET_VISIT", 403),
            (self.vet, "DELIVERY_PROOF", 403),
            (self.owner_a, "VET_VISIT", 403),
            (self.owner_a, "VET_REQUEST", 201),
            (self.admin, "VET_REQUEST", 403),
            (self.admin, "TREATMENT", 201),
        ]
        for user, purpose, expected in matrix:
            self.login_as(user)
            self.assertEqual(self.upload("a.jpg", JPEG, purpose).status_code, expected, (user.username, purpose))

    def test_json_body_is_rejected(self):
        self.login_as(self.admin)
        response = self.client.post("/api/files", {"purpose": "DELIVERY_PROOF"}, format="json")
        self.assertEqual(response.status_code, 415)


class DownloadAndBindingTests(FileTestMixin, BaseAPITestCase):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.logistics = make_user("logist1", Role.LOGISTICS)
        cls.vet = make_user("vet1", Role.VET)

    def make_file(self, user, purpose=AttachmentPurpose.VET_VISIT, name="ko‘rik natijasi.jpg") -> Attachment:
        self.login_as(user)
        response = self.upload(name, JPEG, purpose)
        self.assertEqual(response.status_code, 201, response.data)
        return Attachment.objects.get(pk=response.data["id"])

    def test_download_headers(self):
        attachment = self.make_file(self.vet)
        response = self.client.get(f"/api/files/{attachment.pk}")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(b"".join(response.streaming_content), JPEG)
        self.assertEqual(response["Content-Type"], "image/jpeg")
        self.assertEqual(response["X-Content-Type-Options"], "nosniff")
        self.assertEqual(response["Cache-Control"], "private, no-store")
        self.assertIn("Authorization", response["Vary"])
        self.assertIn("sandbox", response["Content-Security-Policy"])
        self.assertEqual(
            response["Content-Disposition"],
            "inline; filename=\"korik natijasi.jpg\"; filename*=UTF-8''ko%E2%80%98rik%20natijasi.jpg",
        )
        meta = self.client.get(f"/api/files/{attachment.pk}/meta")
        self.assertEqual(meta.data["url"], f"/api/files/{attachment.pk}")

    def test_unbound_file_visible_to_uploader_and_admin_only(self):
        attachment = self.make_file(self.vet)
        for user, expected in ((self.admin, 200), (self.warehouse, 404), (self.owner_a, 404),
                               (make_user("vet2", Role.VET), 404), (self.logistics, 404)):
            self.login_as(user)
            self.assertEqual(self.client.get(f"/api/files/{attachment.pk}/meta").status_code, expected, user.username)
        self.client.force_authenticate(None)
        self.assertEqual(self.client.get(f"/api/files/{attachment.pk}").status_code, 401)

    def test_bind_once_and_visibility_by_household(self):
        attachment = self.make_file(self.vet)
        with transaction.atomic():
            bound = files.bind_files([attachment.pk], actor=self.vet, purpose="VET_VISIT", obj=self.household_a,
                                     household=self.household_a)
        self.assertEqual(len(bound), 1)
        attachment.refresh_from_db()
        self.assertEqual((attachment.object_type, attachment.object_id), ("Household", self.household_a.pk))
        self.assertIsNotNone(attachment.bound_at)
        self.assertEqual(files.files_for(self.household_a), [attachment])
        for user, expected in ((self.owner_a, 200), (self.owner_b, 404), (self.warehouse, 404)):
            self.login_as(user)
            self.assertEqual(self.client.get(f"/api/files/{attachment.pk}").status_code, expected, user.username)
        with self.assertRaises(ValidationError) as ctx:
            files.bind_files([attachment.pk], actor=self.vet, purpose="VET_VISIT", obj=self.household_b)
        self.assertEqual(ctx.exception.detail, {"file_ids": ["Fayl topilmadi yoki allaqachon biriktirilgan."]})

    def test_bind_rules(self):
        own = self.make_file(self.vet)
        other = self.make_file(self.admin)
        wrong_purpose = self.make_file(self.vet, AttachmentPurpose.DISEASE)
        old = self.make_file(self.vet)
        Attachment.objects.filter(pk=old.pk).update(created_at=timezone.now() - timedelta(hours=25))
        for ids in ([other.pk], [wrong_purpose.pk], [old.pk], [own.pk, 999999]):
            with self.subTest(ids=ids), self.assertRaises(ValidationError):
                files.bind_files(ids, actor=self.vet, purpose="VET_VISIT", obj=self.household_a)
        own.refresh_from_db()
        self.assertFalse(own.is_bound)  # nothing bound by a failing call
        self.assertEqual(files.bind_files([], actor=self.vet, purpose="VET_VISIT", obj=self.household_a), [])

    def test_registered_visibility_rule(self):
        attachment = self.make_file(self.vet)
        files.bind_files([attachment.pk], actor=self.vet, purpose="VET_VISIT", obj=self.household_a)
        files.register_visibility("Household", lambda user, row: user.pk == self.logistics.pk)
        try:
            self.login_as(self.logistics)
            self.assertEqual(self.client.get(f"/api/files/{attachment.pk}/meta").status_code, 200)
            self.login_as(self.owner_a)
            self.assertEqual(self.client.get(f"/api/files/{attachment.pk}/meta").status_code, 404)
        finally:
            files._VISIBILITY.pop("Household", None)

    def test_no_public_media_route(self):
        attachment = self.make_file(self.vet)
        self.assertEqual(self.client.get(f"/media/{attachment.file.name}").status_code, 404)

    def test_cleanup_removes_only_old_unbound(self):
        fresh = self.make_file(self.vet)
        old = self.make_file(self.vet)
        bound_old = self.make_file(self.vet)
        files.bind_files([bound_old.pk], actor=self.vet, purpose="VET_VISIT", obj=self.household_a)
        Attachment.objects.filter(pk__in=[old.pk, bound_old.pk]).update(created_at=timezone.now() - timedelta(days=2))
        old_path = Path(self._media, old.file.name)
        self.assertTrue(old_path.exists())
        out = io.StringIO()
        call_command("cleanup_files", stdout=out)
        self.assertIn("O‘chirilgan fayllar: 1", out.getvalue())
        self.assertEqual(set(Attachment.objects.values_list("pk", flat=True)), {fresh.pk, bound_old.pk})
        self.assertFalse(old_path.exists())


class UploadLimitTests(FileTestMixin, BaseAPITestCase):
    """Security findings: body size before parsing, unbound-file quota, photos only from owners."""

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.logistics = make_user("logist_q", Role.LOGISTICS)

    def test_oversized_body_rejected_from_content_length(self):
        self.login_as(self.logistics)
        response = self.client.post(
            "/api/files",
            {"file": SimpleUploadedFile("a.jpg", JPEG), "purpose": "DELIVERY_PROOF"},
            format="multipart",
            CONTENT_LENGTH=str(files.MAX_REQUEST_BYTES + 1),
        )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.data, {"file": ["Fayl hajmi 10 MB dan oshmasligi kerak."]})
        self.assertFalse(Attachment.objects.exists())

    def test_owner_vet_request_accepts_photos_only(self):
        self.login_as(self.owner_a)
        response = self.upload("tahlil.pdf", PDF, "VET_REQUEST")
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.data, {"file": ["Bu yerga faqat JPG, PNG yoki WEBP rasm yuklash mumkin."]})
        self.assertEqual(self.upload("rasm.webp", WEBP, "VET_REQUEST").status_code, 201)
        # Staff purposes keep PDF.
        self.login_as(self.logistics)
        self.assertEqual(self.upload("imzo.pdf", PDF, "DELIVERY_PROOF").status_code, 201)

    def test_unbound_quota_per_user_and_expired_uploads_are_freed(self):
        self.login_as(self.logistics)
        for _ in range(files.MAX_UNBOUND_FILES_PER_USER):
            self.assertEqual(self.upload("a.jpg", JPEG).status_code, 201)
        response = self.upload("a.jpg", JPEG)
        self.assertEqual(response.status_code, 400)
        self.assertIn("Biriktirilmagan fayllaringiz juda ko‘p", response.data["file"][0])
        # Another user is not affected.
        self.login_as(self.admin)
        self.assertEqual(self.upload("a.jpg", JPEG).status_code, 201)
        # Expired unbound uploads of the user are removed on the next upload, freeing the quota.
        Attachment.objects.filter(uploaded_by=self.logistics).update(
            created_at=timezone.now() - files.BIND_WINDOW - timedelta(minutes=1)
        )
        self.login_as(self.logistics)
        self.assertEqual(self.upload("a.jpg", JPEG).status_code, 201)
        self.assertEqual(Attachment.objects.filter(uploaded_by=self.logistics).count(), 1)


class StockDocumentTests(FileTestMixin, BaseAPITestCase):
    """TZ §7: a stock IN/OUT carries the scan of its document (purpose STOCK_DOCUMENT)."""

    def test_in_with_document(self):
        from common.testing import make_product

        product = make_product("Yem PK-7", min_stock="0")
        self.login_as(self.warehouse)
        upload = self.upload("yuk-xati.pdf", PDF, "STOCK_DOCUMENT")
        self.assertEqual(upload.status_code, 201, upload.data)
        response = self.client.post("/api/stock-transactions", {
            "product": product.pk, "type": "IN", "quantity": "100", "document_no": "YX-77",
            "file_ids": [upload.data["id"]],
        }, format="json")
        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual([item["id"] for item in response.data["files"]], [upload.data["id"]])
        listed = self.client.get(f"/api/stock-transactions?product={product.pk}").data["results"][0]
        self.assertEqual(listed["files"][0]["name"], "yuk-xati.pdf")
        self.assertEqual(self.client.get(f"/api/files/{upload.data['id']}").status_code, 200)
        # A file is bound once only; owners / vets cannot upload stock documents.
        again = self.client.post("/api/stock-transactions", {
            "product": product.pk, "type": "IN", "quantity": "1", "document_no": "YX-78",
            "file_ids": [upload.data["id"]],
        }, format="json")
        self.assertEqual(again.status_code, 400)
        self.login_as(self.owner_a)
        self.assertEqual(self.upload("a.jpg", JPEG, "STOCK_DOCUMENT").status_code, 403)

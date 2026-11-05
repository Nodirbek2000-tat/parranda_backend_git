"""``POST /api/bot/files`` throttling: ``uploads`` per linked user, not per bot IP (API_CONTRACT_V3 §4.2, §14.1)."""

from __future__ import annotations

from unittest import mock

from django.core.cache import cache
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import override_settings

from common.testing import BOT_API_TOKEN
from core.models import Attachment
from logistics.views import BotUploadThrottle

from .helpers import PNG, LogisticsTestCase


@override_settings(BOT_API_TOKEN=BOT_API_TOKEN)
class BotUploadThrottleTests(LogisticsTestCase):
    def setUp(self):
        super().setUp()
        cache.clear()
        self.addCleanup(cache.clear)

    def upload(self, telegram_id):
        return self.client.post(
            "/api/bot/files",
            {"telegram_id": telegram_id, "purpose": "DELIVERY_PROOF", "file": SimpleUploadedFile("dalil.png", PNG)},
            format="multipart",
            HTTP_X_BOT_TOKEN=BOT_API_TOKEN,
        )

    def test_each_linked_user_has_own_bucket(self):
        with mock.patch.object(BotUploadThrottle, "THROTTLE_RATES", {"uploads": "2/hour"}):
            self.assertEqual(self.upload(7001).status_code, 201)
            self.assertEqual(self.upload(7001).status_code, 201)
            self.assertEqual(self.upload(7001).status_code, 429)
            # Another driver behind the same bot (same IP) is not affected.
            response = self.upload(7002)
            self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(Attachment.objects.filter(uploaded_by=self.logist).count(), 2)
        self.assertEqual(Attachment.objects.filter(uploaded_by=self.logist2).count(), 1)

    def test_bucket_is_shared_with_the_users_web_uploads(self):
        self.assertEqual(
            BotUploadThrottle().get_cache_key(mock.Mock(data={"telegram_id": "7001"}), None),
            f"throttle_uploads_{self.logist.pk}",
        )

    def test_unknown_or_bad_telegram_id_is_not_throttled_and_rejected_by_the_view(self):
        throttle = BotUploadThrottle()
        for raw in ("", "abc", "-5", "0", str(2**63), "123456789"):
            with self.subTest(raw=raw):
                self.assertIsNone(throttle.get_cache_key(mock.Mock(data={"telegram_id": raw}), None))
        self.assertEqual(self.upload(123456789).status_code, 404)
        self.assertEqual(self.upload("abc").status_code, 400)
        self.assertFalse(Attachment.objects.filter(uploaded_by__telegram_id=123456789).exists())

    def test_owner_cannot_upload_delivery_proof_through_the_bot(self):
        self.owner_a.telegram_id = 9001
        self.owner_a.save(update_fields=["telegram_id"])
        self.assertEqual(self.upload(9001).status_code, 403)
        self.assertFalse(Attachment.objects.filter(uploaded_by=self.owner_a).exists())

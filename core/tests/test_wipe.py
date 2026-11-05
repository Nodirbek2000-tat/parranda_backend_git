"""``POST /api/system/wipe``: Super Admin deletes everything except his own account (and can restore it)."""

from __future__ import annotations

import io
import shutil
import tempfile
from pathlib import Path
from unittest import mock

from django.core.management import call_command
from django.test import override_settings
from django.utils import timezone
from rest_framework.test import APITestCase

from accounts.models import User
from audit.models import AuditLog
from builder.models import Blueprint
from common.roles import Role
from common.testing import make_user
from core import wipe
from farm.management.commands import seed_demo
from farm.models import Household
from logistics.models import LogisticsTask
from vet.models import VetVisit
from warehouse.models import Product

URL = "/api/system/wipe"
PASSWORD = "Parol12345"


class SystemWipeTests(APITestCase):
    @classmethod
    def setUpClass(cls) -> None:
        super().setUpClass()
        cls._media = tempfile.mkdtemp(prefix="nnp-wipe-")
        cls._override = override_settings(MEDIA_ROOT=cls._media)
        cls._override.enable()

    @classmethod
    def tearDownClass(cls) -> None:
        cls._override.disable()
        shutil.rmtree(cls._media, ignore_errors=True)
        super().tearDownClass()

    def setUp(self) -> None:
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        patcher = mock.patch.object(seed_demo, "CREDENTIALS_FILE", Path(folder.name) / "creds.txt")
        patcher.start()
        self.addCleanup(patcher.stop)
        self.boss = make_user("boss", Role.ADMIN, is_superuser=True, is_staff=True)
        self.other_super = make_user("boss2", Role.ADMIN, is_superuser=True, is_staff=True)
        call_command("seed_demo", stdout=io.StringIO())
        call_command("seed_builder_examples", stdout=io.StringIO())
        self.client.force_authenticate(self.boss)

    def post(self, password=PASSWORD, confirm=wipe.CONFIRM_TEXT):
        return self.client.post(URL, {"password": password, "confirm": confirm}, format="json")

    def test_wrong_password_or_text_deletes_nothing(self):
        households = Household.objects.count()
        self.assertGreater(households, 0)
        self.assertEqual(self.post(password="xato").status_code, 400)
        self.assertEqual(self.post(confirm="ha").status_code, 400)
        self.assertEqual(Household.objects.count(), households)

    def test_only_super_admin(self):
        admin = make_user("oddiy_admin", Role.ADMIN)
        self.client.force_authenticate(admin)
        self.assertEqual(self.post().status_code, 403)
        self.assertTrue(Household.objects.exists())

    def test_wipe_keeps_only_the_actor_and_can_be_restored(self):
        counts = {model: model.objects.count() for model in (Household, Product, VetVisit, LogisticsTask, Blueprint)}
        self.assertTrue(all(counts.values()), counts)
        users_before = User.objects.count()

        # A soft-deleted household (hidden by the default manager) must be wiped and backed up too.
        hidden = Household.objects.first()
        Household.all_objects.filter(pk=hidden.pk).update(deleted_at=timezone.now())
        response = self.post()
        self.assertEqual(response.status_code, 200, response.data)
        self.assertFalse(Household.all_objects.exists())
        self.assertEqual(list(User.objects.values_list("username", flat=True)), ["boss"])
        for model in counts:
            self.assertFalse(model.objects.exists(), model.__name__)
        self.assertTrue(AuditLog.objects.filter(after__wipe=True).exists())
        backup = Path(self._media) / "system-backups" / response.data["backup"]
        self.assertTrue(backup.exists())

        # The snapshot restores everything (the actor row is simply rewritten).
        call_command("loaddata", str(backup), verbosity=0)
        self.assertEqual(User.objects.count(), users_before)
        for model, count in counts.items():
            expected = count - 1 if model is Household else count  # the hidden one stays hidden
            self.assertEqual(model.objects.count(), expected, model.__name__)
        self.assertTrue(Household.all_objects.filter(pk=hidden.pk).exists())

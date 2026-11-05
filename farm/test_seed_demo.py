"""``manage.py seed_demo``: ~60 days of consistent demo history, no audit rows, ``--reset`` supported."""

from __future__ import annotations

import io
import tempfile
from pathlib import Path
from unittest import mock

from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase

from accounts.models import User
from audit.models import AuditLog
from farm.management.commands import seed_demo
from stats import services as stats_services

from .models import DailyReport, Household, PoultryBatch, PoultryMovement


class SeedDemoTests(TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.credentials = Path(folder.name) / "demo_credentials.txt"
        patcher = mock.patch.object(seed_demo, "CREDENTIALS_FILE", self.credentials)
        patcher.start()
        self.addCleanup(patcher.stop)

    def seed(self, *args) -> str:
        out = io.StringIO()
        call_command("seed_demo", *args, stdout=out)
        return out.getvalue()

    def test_seed_reset_and_consistency(self):
        output = self.seed()
        self.assertEqual(output.strip(), str(self.credentials))  # only the file path is printed
        self.assertEqual(Household.objects.count(), 12)
        self.assertEqual(Household.objects.filter(is_active=False).count(), 1)
        self.assertEqual(PoultryBatch.objects.count(), 5)
        self.assertGreater(DailyReport.objects.count(), 400)
        oldest = DailyReport.objects.order_by("date").first().date
        newest = DailyReport.objects.order_by("-date").first().date
        self.assertGreaterEqual((newest - oldest).days, 55)
        self.assertEqual(AuditLog.objects.count(), 0)  # management commands are not audited
        self.assertIn("demo_admin", self.credentials.read_text(encoding="utf-8"))
        self.assertFalse(User.objects.filter(username__startswith="demo_", must_change_password=True).exists())

        integrity = io.StringIO()
        call_command("check_integrity", stdout=integrity)
        self.assertIn("Nomuvofiqlik topilmadi.", integrity.getvalue())

        overview = stats_services.overview({})
        self.assertEqual(overview["households"]["total"], 12)
        self.assertGreater(overview["kpis"]["meat_kg"]["value"], 0)
        self.assertIsNotNone(overview["kpis"]["fcr"]["previous"])
        self.assertIsNotNone(overview["kpis"]["avg_weight_g"]["value"])
        self.assertEqual(set(PoultryBatch.objects.values_list("poultry_type", flat=True)), {"BROILER", "BREEDER"})
        self.assertFalse(DailyReport.objects.filter(meat_kg__isnull=False, meat_kg__lte=0).exists())
        self.assertEqual(len(overview["by_district"]), 3)
        self.assertGreaterEqual(len(overview["top_households"]), 5)
        self.assertGreaterEqual(overview["high_mortality"]["count"], 1)

        with self.assertRaises(CommandError):
            self.seed()
        movements_before = PoultryMovement.objects.count()
        self.seed("--reset")
        self.assertEqual(Household.objects.count(), 12)
        self.assertEqual(PoultryMovement.objects.count(), movements_before)  # deterministic (seeded RNG)


class SeedDemoV3Tests(TestCase):
    """TZ §18: the seed also covers logistics, orders, veterinary and lots; households are marked demo."""

    def setUp(self):
        import shutil

        from django.test import override_settings

        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        media = tempfile.mkdtemp(prefix="nnp-seed-media-")
        self.addCleanup(shutil.rmtree, media, True)
        override = override_settings(MEDIA_ROOT=media, FILE_STORAGE="local")
        override.enable()
        self.addCleanup(override.disable)
        patcher = mock.patch.object(seed_demo, "CREDENTIALS_FILE", Path(folder.name) / "demo_credentials.txt")
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_v3_data_and_demo_flag(self):
        from logistics.models import LogisticsTask
        from orders.models import Order
        from vet.models import DiseaseRecord, Treatment, Vaccination, VetVisit
        from warehouse.models import StockLot

        call_command("seed_demo", stdout=io.StringIO())
        self.assertEqual(Household.objects.filter(is_demo=False).count(), 0)
        self.assertTrue(User.objects.filter(username="demo_logist", role="LOGISTICS").exists())
        self.assertTrue(User.objects.filter(username="demo_vet", role="VET").exists())
        self.assertEqual(set(Order.objects.values_list("status", flat=True)), {"NEW", "APPROVED", "REJECTED"})
        self.assertTrue({"NEW", "ASSIGNED", "IN_TRANSIT", "CONFIRMED"} <= set(LogisticsTask.objects.values_list("status", flat=True)))
        self.assertEqual(VetVisit.objects.count(), 2)
        self.assertEqual(Vaccination.objects.count(), 3)
        self.assertTrue(DiseaseRecord.objects.exists() and Treatment.objects.exists())
        self.assertTrue(StockLot.objects.filter(expiry_date__isnull=False).exists())
        integrity = io.StringIO()
        call_command("check_integrity", stdout=integrity)
        self.assertIn("Nomuvofiqlik topilmadi.", integrity.getvalue())
        # --reset wipes the V3 rows too and seeds again.
        call_command("seed_demo", "--reset", stdout=io.StringIO())
        self.assertEqual(VetVisit.objects.count(), 2)

"""``manage.py check_integrity``: recomputes counts / balances and fails (exit 1) on any inconsistency."""

from __future__ import annotations

import io
from datetime import timedelta
from decimal import Decimal

from django.core.management import call_command
from django.core.management.base import CommandError
from django.utils import timezone

from common.testing import BaseAPITestCase, make_product
from farm import services as farm_services
from warehouse import services as warehouse_services
from warehouse.models import Product, StockTransaction

from .models import DailyReport, MovementType, PoultryMovement


class CheckIntegrityTests(BaseAPITestCase):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.product = make_product()
        for kind, quantity in (("IN", "100.50"), ("OUT", "40.25"), ("IN", "5")):
            warehouse_services.record_stock_transaction(
                actor=cls.warehouse, product_id=cls.product.pk, transaction_type=kind, quantity=Decimal(quantity),
                document_no="D-1",
            )
        cls.report = farm_services.create_daily_report(
            actor=cls.owner_a, household=None, feed_kg=Decimal("10"), eggs=100, deaths=2
        )
        farm_services.record_movement(
            actor=cls.admin, batch=cls.batch, household=None, movement_type=MovementType.OUT, quantity=5
        )

    def run_command(self) -> str:
        out = io.StringIO()
        call_command("check_integrity", stdout=out)
        return out.getvalue()

    def failures(self) -> str:
        out = io.StringIO()
        with self.assertRaises(CommandError) as ctx:
            call_command("check_integrity", stdout=out)
        self.assertEqual(ctx.exception.returncode, 1)
        return out.getvalue()

    def test_consistent_data_passes(self):
        output = self.run_command()
        self.assertIn("Tekshirildi: 1 partiya, 2 xonadon, 1 kunlik hisobot, 1 mahsulot, 3 ombor operatsiyasi.", output)
        self.assertIn("Nomuvofiqlik topilmadi.", output)

    def test_product_balance_drift(self):
        Product.objects.filter(pk=self.product.pk).update(balance=Decimal("70.00"))
        self.assertIn("Mahsulot “Yem PK-1”: qoldiq 70.00 ≠ operatsiyalardan 65.25", self.failures())

    def test_broken_balance_after_chain(self):
        tx = StockTransaction.objects.filter(product=self.product).order_by("id")[1]
        StockTransaction.objects.filter(pk=tx.pk).update(balance_after=Decimal("61.00"))
        self.assertIn(f"Operatsiya {tx.op_id}: balance_after 61.00 ≠ qayta hisoblangan 60.25", self.failures())

    def test_negative_household_count_and_invariant(self):
        # Inserted directly (bypassing the service checks) to simulate corrupted data.
        PoultryMovement.objects.create(batch=self.batch, household=self.household_b, type=MovementType.RETURN,
                                       quantity=250, date=timezone.localdate())
        output = self.failures()
        self.assertIn(f"Partiya {self.batch.code}, xonadon {self.household_b.code}: soni manfiy (-50).", output)
        self.assertIn(f"Xonadon {self.household_b.code}: parranda soni manfiy (-50).", output)

    def test_negative_center_and_bad_dates(self):
        PoultryMovement.objects.create(batch=self.batch, household=None, type=MovementType.OUT, quantity=600,
                                       date=self.batch.arrived_at - timedelta(days=1))
        output = self.failures()
        self.assertIn("markazdagi soni manfiy (-105)", output)
        self.assertIn("partiya", output)

    def test_report_without_matching_death_movement(self):
        DailyReport.objects.filter(pk=self.report.pk).update(deaths=3)
        self.assertIn(f"bog‘langan {self.report.death_movement.op_id} hisobotga mos emas", self.failures())
        DailyReport.objects.filter(pk=self.report.pk).update(deaths=0)
        self.assertIn("nobud 0, lekin", self.failures())

    def test_back_dated_movement_gives_a_warning_not_a_failure(self):
        """LEGACY data: a death booked before the birds reached the household (Phase 1 accepted it)."""
        late = farm_services.create_batch(
            actor=self.admin, poultry_type="BROILER", breed="Ross 308", initial_quantity=100, age_days=1,
            supplier="S", arrived_at=timezone.localdate() - timedelta(days=10),
        )
        farm_services.record_movement(actor=self.admin, batch=late, household=self.household_a,
                                      movement_type=MovementType.DISTRIBUTE, quantity=50,
                                      on_date=timezone.localdate() - timedelta(days=2))
        # The service now rejects it; old databases may still contain such rows (inserted directly here).
        PoultryMovement.objects.create(batch=late, household=self.household_a, type=MovementType.DEATH, quantity=5,
                                       date=timezone.localdate() - timedelta(days=5), created_by=self.admin)
        output = self.run_command()
        day = (timezone.localdate() - timedelta(days=5)).strftime("%d.%m.%Y")
        self.assertIn(f"! Partiya {late.code}, xonadon {self.household_a.code}: {day} kuni oxirida soni manfiy (-5)",
                      output)
        self.assertIn("Nomuvofiqlik topilmadi.", output)

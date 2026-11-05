"""``problem_households`` / ``expiring_lots`` of ``/api/dashboard/admin`` (API_CONTRACT_V3 §10.1)."""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

from django.db import connection
from django.test.utils import CaptureQueriesContext

from common.roles import Role
from common.testing import make_household, make_product, make_user
from farm.models import MovementType
from stats.tests.helpers import TODAY, FrozenClockTestCase, batch, created_on, move, report, staff
from vet.models import DiseaseRecord, Vaccination
from warehouse import services as warehouse_services


class ProblemHouseholdTests(FrozenClockTestCase):
    @classmethod
    def setUpTestData(cls):
        cls.admin, cls.warehouse = staff()
        layer = batch(cls.admin, 5000, date(2026, 9, 1))
        # quiet: 3 days without a report (problem_report_days = 2 by default)
        cls.quiet = make_household("Jim 1-uy", "owner_q")
        created_on(cls.quiet, date(2026, 9, 1))
        move(cls.admin, layer, cls.quiet, MovementType.DISTRIBUTE, 100, date(2026, 10, 1))
        report(cls.quiet, TODAY - timedelta(days=3), 50, "10.00")
        # dying: 10 deaths of 110 in 7 days (9.1 % > 5 %), reported today
        cls.dying = make_household("O‘lim 2-uy", "owner_d")
        created_on(cls.dying, date(2026, 9, 1))
        move(cls.admin, layer, cls.dying, MovementType.DISTRIBUTE, 110, date(2026, 10, 1))
        move(cls.admin, layer, cls.dying, MovementType.DEATH, 10, TODAY - timedelta(days=2))
        report(cls.dying, TODAY, 60, "11.00")
        # never reported since the distribution 5 days ago + an overdue vaccination + an open disease
        cls.worst = make_household("Yomon 3-uy", "owner_w")
        created_on(cls.worst, date(2026, 9, 1))
        move(cls.admin, layer, cls.worst, MovementType.DISTRIBUTE, 50, TODAY - timedelta(days=5))
        Vaccination.objects.create(household=cls.worst, vaccine_name="ND", planned_date=TODAY - timedelta(days=1))
        DiseaseRecord.objects.create(household=cls.worst, disease_name="Kolibakterioz", diagnosed_date=TODAY, cases=3)
        # fine: reported today, no deaths
        cls.fine = make_household("Yaxshi 4-uy", "owner_f")
        created_on(cls.fine, date(2026, 9, 1))
        move(cls.admin, layer, cls.fine, MovementType.DISTRIBUTE, 100, date(2026, 10, 1))
        report(cls.fine, TODAY, 70, "10.00")
        # expiring lot
        feed = make_product("Vaksina ND", min_stock="0")
        warehouse_services.record_stock_transaction(
            actor=cls.warehouse, product_id=feed.pk, transaction_type="IN", quantity=Decimal("10"),
            document_no="V-1", lot_no="L-9", expiry_date=TODAY + timedelta(days=4),
        )

    def test_rules_and_order(self):
        self.login_as(self.admin)
        data = self.client.get("/api/dashboard/admin").data
        problems = data["problem_households"]
        self.assertEqual(problems["count"], 3)
        items = problems["items"]
        self.assertEqual([item["household"]["id"] for item in items], [self.worst.pk, self.quiet.pk, self.dying.pk])
        worst = {reason["code"]: reason for reason in items[0]["reasons"]}
        self.assertEqual(set(worst), {"NO_REPORT", "OVERDUE_VACCINATION", "OPEN_DISEASE"})
        self.assertEqual(worst["NO_REPORT"]["value"], 5)
        self.assertEqual(worst["NO_REPORT"]["label"], "5 kundan beri hisobot yo‘q")
        self.assertEqual(worst["OVERDUE_VACCINATION"]["label"], "Muddati o‘tgan emlash: 1")
        self.assertEqual(items[1]["reasons"], [{"code": "NO_REPORT", "label": "3 kundan beri hisobot yo‘q", "value": 3}])
        self.assertEqual(items[2]["reasons"], [{"code": "HIGH_MORTALITY", "label": "7 kunlik o‘lim 9.1%", "value": 9.1}])
        self.assertEqual(items[2]["district"], "Navobod tumani")
        lots = data["expiring_lots"]
        self.assertEqual(lots["count"], 1)
        self.assertEqual(lots["items"][0]["lot_no"], "L-9")
        self.assertEqual(lots["items"][0]["status"], "EXPIRING")

    def test_query_budget(self):
        self.login_as(self.admin)
        self.client.get("/api/dashboard/admin")
        with CaptureQueriesContext(connection) as ctx:
            response = self.client.get("/api/dashboard/admin")
        self.assertEqual(response.status_code, 200)
        self.assertLessEqual(len(ctx.captured_queries), 22, [q["sql"][:90] for q in ctx.captured_queries])

    def test_role_access(self):
        self.login_as(make_user("vet1", Role.VET))
        self.assertEqual(self.client.get("/api/dashboard/admin").status_code, 403)

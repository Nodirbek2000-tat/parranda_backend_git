"""Verifier edge cases: back-dated deaths that make a per-type count negative on past days."""

from __future__ import annotations

from datetime import date

from rest_framework.exceptions import ValidationError

from farm.models import MovementType, PoultryMovement, PoultryType

from .helpers import FrozenClockTestCase, batch, created_on, move, report, staff
from common.testing import make_household


class NegativePastCountTests(FrozenClockTestCase):
    @classmethod
    def setUpTestData(cls):
        cls.admin, cls.warehouse = staff()
        cls.household = make_household("Navbahor", "owner_neg")
        created_on(cls.household, date(2026, 10, 1))
        layer = batch(cls.admin, 1000, date(2026, 10, 1))
        broiler = batch(cls.admin, 1000, date(2026, 10, 1), PoultryType.BROILER)
        move(cls.admin, layer, cls.household, MovementType.DISTRIBUTE, 100, date(2026, 10, 20))
        move(cls.admin, broiler, cls.household, MovementType.DISTRIBUTE, 100, date(2026, 11, 3))
        # LEGACY data: a back-dated death of the broiler batch BEFORE it was distributed. Phase 1 accepted it
        # (current count 100 ≥ 100); the service now rejects it (see test_service_rejects_it), so it is
        # inserted directly — the statistics must still survive such rows in old databases.
        PoultryMovement.objects.create(batch=broiler, household=cls.household, type=MovementType.DEATH,
                                       quantity=100, date=date(2026, 11, 1), created_by=cls.admin)
        cls.broiler = broiler
        report(cls.household, date(2026, 11, 1), 80, "10.00")
        report(cls.household, date(2026, 11, 2), 85, "11.00")

    def test_service_rejects_it(self):
        with self.assertRaises(ValidationError) as ctx:
            move(self.admin, self.broiler, self.household, MovementType.DEATH, 1, date(2026, 11, 2))
        self.assertEqual(ctx.exception.detail["quantity"][0][:32], "Yetarli parranda yo‘q. Mavjud: 0")

    def test_household_stats_do_not_crash(self):
        self.login_as(self.admin)
        response = self.client.get(f"/api/households/{self.household.pk}/stats?date_from=2026-11-01&date_to=2026-11-03")
        self.assertEqual(response.status_code, 200, response.content[:300])

    def test_overview_does_not_crash(self):
        self.login_as(self.admin)
        response = self.client.get("/api/stats/overview?date_from=2026-11-01&date_to=2026-11-03")
        self.assertEqual(response.status_code, 200, response.content[:300])

    def test_values_with_negative_past_count(self):
        """11-01 and 11-02: layers 100, broilers −100 → B = 0; feed share capped at 1 (all feed to layers)."""
        self.login_as(self.admin)
        data = self.client.get(
            f"/api/households/{self.household.pk}/stats?date_from=2026-11-01&date_to=2026-11-03"
        ).json()
        kpis = data["kpis"]
        self.assertEqual(kpis["laying_rate_pct"]["value"], 82.5)  # 165 / 200
        self.assertEqual(kpis["feed_per_egg_g"]["value"], 127.3)  # 1000 × 21 / 165
        self.assertIsNone(kpis["feed_per_bird_day_kg"]["value"])  # no reported day with B > 0
        self.assertEqual([row["birds"] for row in data["series"]], [0, 0, 100])
        self.assertEqual(data["missing_days"], ["2026-11-03"])
        self.assertEqual(data["discipline"], {"expected": 1, "reported": 0, "missing": 1})

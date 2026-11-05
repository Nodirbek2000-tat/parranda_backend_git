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
        breeder = batch(cls.admin, 1000, date(2026, 10, 1))
        broiler = batch(cls.admin, 1000, date(2026, 10, 1), PoultryType.BROILER)
        move(cls.admin, breeder, cls.household, MovementType.DISTRIBUTE, 100, date(2026, 10, 20))
        move(cls.admin, broiler, cls.household, MovementType.DISTRIBUTE, 100, date(2026, 11, 3))
        # LEGACY data: a back-dated death of the broiler batch BEFORE it was distributed. Phase 1 accepted it
        # (current count 100 ≥ 100); the service now rejects it (see test_service_rejects_it), so it is
        # inserted directly — the statistics must still survive such rows in old databases.
        PoultryMovement.objects.create(batch=broiler, household=cls.household, type=MovementType.DEATH,
                                       quantity=100, date=date(2026, 11, 1), created_by=cls.admin)
        cls.broiler = broiler
        report(cls.household, date(2026, 11, 1), "8.00", "10.00", weight="1500")
        report(cls.household, date(2026, 11, 2), "9.00", "11.00", weight="1600")

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
        """11-01 and 11-02: breeders 100, broilers −100 → B = 0: meat / FCR still count, weights do not."""
        self.login_as(self.admin)
        data = self.client.get(
            f"/api/households/{self.household.pk}/stats?date_from=2026-11-01&date_to=2026-11-03"
        ).json()
        kpis = data["kpis"]
        self.assertEqual(kpis["meat_kg"]["value"], 17.0)
        self.assertEqual(kpis["fcr"]["value"], 1.24)  # 21 / 17 = 1.235…
        self.assertIsNone(kpis["avg_weight_g"]["value"])  # weights of days with B = 0 are not averaged
        self.assertEqual(kpis["meat_per_bird_kg"]["value"], 0.51)  # 17 kg / (100 bird-days / 3 days)
        self.assertIsNone(kpis["feed_per_bird_day_kg"]["value"])  # no reported day with B > 0
        self.assertEqual([row["birds"] for row in data["series"]], [0, 0, 100])
        self.assertEqual(data["missing_days"], ["2026-11-03"])
        self.assertEqual(data["discipline"], {"expected": 1, "reported": 0, "missing": 1})

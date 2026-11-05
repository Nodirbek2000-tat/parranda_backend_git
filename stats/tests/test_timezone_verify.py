"""Verifier: operations made at 23:30 / 00:15 Tashkent time land on the local day everywhere (API → stats)."""

from __future__ import annotations

from datetime import date, datetime, time
from unittest import mock

from common.testing import make_household, make_product
from farm.models import DailyReport, PoultryMovement

from .helpers import TASHKENT, FrozenClockTestCase, batch, created_on, staff


def at(day: date, hour: int, minute: int) -> datetime:
    return datetime.combine(day, time(hour, minute), tzinfo=TASHKENT)


class LocalDayBoundaryTests(FrozenClockTestCase):
    @classmethod
    def setUpTestData(cls):
        cls.admin, cls.warehouse = staff()
        cls.household = make_household("Navbahor", "owner_tz")
        created_on(cls.household, date(2026, 10, 1))
        cls.batch = batch(cls.admin, 1000, date(2026, 10, 1))
        cls.product = make_product("Yem TZ", min_stock="0")

    def post_at(self, moment: datetime, url: str, body: dict, user=None):
        self.login_as(user or self.admin)
        with mock.patch("django.utils.timezone.now", return_value=moment):
            response = self.client.post(url, body, format="json")
        self.assertIn(response.status_code, (200, 201), response.content[:300])
        return response.json()

    def test_2330_local_belongs_to_that_day(self):
        late = at(date(2026, 11, 2), 23, 30)  # = 18:30Z on 02.11
        early = at(date(2026, 11, 3), 0, 15)  # = 19:15Z on 02.11 (UTC day still 02.11!)
        self.post_at(late, "/api/movements",
                     {"batch": self.batch.pk, "household": self.household.pk, "type": "DISTRIBUTE", "quantity": 100})
        self.post_at(early, "/api/movements",
                     {"batch": self.batch.pk, "household": self.household.pk, "type": "DEATH", "quantity": 3})
        self.assertEqual(
            sorted(PoultryMovement.objects.values_list("date", flat=True)), [date(2026, 11, 2), date(2026, 11, 3)]
        )
        self.post_at(late, "/api/daily-reports", {"household": self.household.pk, "eggs": 50, "feed_kg": "10.00"})
        self.post_at(early, "/api/daily-reports", {"household": self.household.pk, "eggs": 60, "feed_kg": "11.00"})
        self.assertEqual(
            sorted(DailyReport.objects.values_list("date", flat=True)), [date(2026, 11, 2), date(2026, 11, 3)]
        )
        self.post_at(late, "/api/stock-transactions",
                     {"product": self.product.pk, "type": "IN", "quantity": "500", "document_no": "N1"})
        self.post_at(early, "/api/stock-transactions",
                     {"product": self.product.pk, "type": "OUT", "quantity": "40", "document_no": "N2",
                      "household": self.household.pk})

        self.login_as(self.admin)
        data = self.client.get(
            f"/api/households/{self.household.pk}/stats?date_from=2026-11-02&date_to=2026-11-03"
        ).json()
        self.assertEqual([r["birds"] for r in data["series"]], [100, 97])
        self.assertEqual([r["eggs"] for r in data["series"]], [50, 60])
        self.assertEqual([r["deaths"] for r in data["series"]], [0, 3])

        product = self.client.get(f"/api/products/{self.product.pk}/stats?date_from=2026-11-02&date_to=2026-11-03").json()
        self.assertEqual([(r["in_qty"], r["out_qty"], r["balance"]) for r in product["series"]],
                         [(500.0, 0.0, 500.0), (0.0, 40.0, 460.0)])
        one_day = self.client.get(f"/api/products/{self.product.pk}/stats?date_from=2026-11-03&date_to=2026-11-03").json()
        self.assertEqual(one_day["totals"]["opening_balance"], 500.0)
        self.assertEqual(one_day["totals"]["out_count"], 1)

        warehouse = self.client.get("/api/warehouse/stats?date_from=2026-11-02&date_to=2026-11-03").json()
        self.assertEqual([(r["in_count"], r["out_count"]) for r in warehouse["series"]], [(1, 0), (0, 1)])

        # audit journal date filter uses the local day of created_at as well
        count = lambda q: self.client.get(f"/api/audit-logs?module=stock&{q}").json()["count"]  # noqa: E731
        self.assertEqual(count("date_from=2026-11-02&date_to=2026-11-02"), 1)
        self.assertEqual(count("date_from=2026-11-03&date_to=2026-11-03"), 1)

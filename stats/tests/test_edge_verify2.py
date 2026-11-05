"""Verifier edge cases: audit-log date filters at the calendar limits, period validation details."""

from common.roles import Role
from common.testing import make_household, make_user

from .helpers import FrozenClockTestCase


class AuditDateEdgeTests(FrozenClockTestCase):
    @classmethod
    def setUpTestData(cls):
        cls.admin = make_user("admin_x", Role.ADMIN)

    def test_extreme_dates_are_400_not_500(self):
        """``0001-01-01`` / ``9999-12-31`` used to overflow in ``day_range`` (500)."""
        self.login_as(self.admin)
        cases = {
            "date_from=0001-01-01": {"date_from": ["Sana 01.01.2000 dan oldin bo‘lishi mumkin emas."]},
            "date_from=1999-12-31&date_to=2026-11-04": {"date_from": ["Sana 01.01.2000 dan oldin bo‘lishi mumkin emas."]},
            "date_to=9999-12-31": {"date_to": ["Sana kelajakda bo‘lishi mumkin emas"]},
            "date_to=2026-11-05": {"date_to": ["Sana kelajakda bo‘lishi mumkin emas"]},
            "date_from=0001-01-01&date_to=0001-01-02": {"date_from": ["Sana 01.01.2000 dan oldin bo‘lishi mumkin emas."]},
            "date_from=2026-11-04&date_to=2026-11-03": {
                "date_from": ["Boshlanish sanasi tugash sanasidan keyin bo‘lishi mumkin emas."]
            },
            "date_from=2026-13-01": {"date_from": ["Sana noto‘g‘ri formatda (YYYY-MM-DD)."]},
        }
        for query, body in cases.items():
            with self.subTest(query=query):
                response = self.client.get(f"/api/audit-logs?{query}")
                self.assertEqual(response.status_code, 400, response.content[:200])
                self.assertEqual(response.json(), body)
        for query in ("date_to=0001-01-01", "date_from=2000-01-01", "date_from=2000-01-01&date_to=2026-11-04",
                      "date_from=2026-11-04"):
            with self.subTest(query=query):
                self.assertEqual(self.client.get(f"/api/audit-logs?{query}").status_code, 200)


class PeriodValidationEdgeTests(FrozenClockTestCase):
    @classmethod
    def setUpTestData(cls):
        cls.admin = make_user("admin_y", Role.ADMIN)
        cls.household = make_household("Navbahor", "owner_pv")

    def test_period_errors(self):
        self.login_as(self.admin)
        urls = ["/api/stats/overview", "/api/warehouse/stats", f"/api/households/{self.household.pk}/stats"]
        cases = {
            "date_from=2026-11-4": {"date_from": ["Sana noto‘g‘ri formatda (YYYY-MM-DD)."]},
            "date_from=2026-02-30&date_to=x": {"date_from": ["Sana noto‘g‘ri formatda (YYYY-MM-DD)."],
                                                "date_to": ["Sana noto‘g‘ri formatda (YYYY-MM-DD)."]},
            "date_to=2026-11-05": {"date_to": ["Sana kelajakda bo‘lishi mumkin emas"]},
            "date_from=2026-11-05": {"date_from": ["Boshlanish sanasi tugash sanasidan keyin bo‘lishi mumkin emas."]},
            "date_from=2026-11-06&date_to=2026-11-05": {
                "date_to": ["Sana kelajakda bo‘lishi mumkin emas"],
                "date_from": ["Boshlanish sanasi tugash sanasidan keyin bo‘lishi mumkin emas."],
            },
            "date_from=1999-12-31&date_to=2000-01-05": {"date_from": ["Sana 01.01.2000 dan oldin bo‘lishi mumkin emas."]},
            "date_from=2025-11-03": {"non_field_errors": ["Davr 366 kundan oshmasligi kerak."]},
            "date_to=0001-01-01": {"date_from": ["Sana 01.01.2000 dan oldin bo‘lishi mumkin emas."]},
        }
        for url in urls:
            for query, body in cases.items():
                with self.subTest(url=url, query=query):
                    response = self.client.get(f"{url}?{query}")
                    self.assertEqual(response.status_code, 400, response.content[:200])
                    self.assertEqual(response.json(), body)
            for query, days in (("date_from=2025-11-04", 366), ("", 30), ("date_to=2026-01-10", 30),
                                ("date_from=2000-01-01&date_to=2000-01-01", 1), ("unknown=1", 30)):
                with self.subTest(url=url, query=query):
                    response = self.client.get(f"{url}?{query}")
                    self.assertEqual(response.status_code, 200, response.content[:200])
                    self.assertEqual(response.json()["period"]["days"], days)
                    self.assertEqual(len(response.json()["series"]), days)

    def test_batch_and_product_period_errors(self):
        from datetime import date

        from common.testing import make_product

        from .helpers import batch

        b = batch(self.admin, 100, date(2026, 10, 1))
        product = make_product()
        self.login_as(self.admin)
        for url in (f"/api/batches/{b.pk}/stats", f"/api/products/{product.pk}/stats"):
            for query, field in (("date_to=0001-01-01", "date_from"), ("date_to=0001-12-31", "date_from"),
                                 ("date_from=2025-11-03", "non_field_errors"), ("date_to=2026-11-05", "date_to")):
                with self.subTest(url=url, query=query):
                    response = self.client.get(f"{url}?{query}")
                    self.assertEqual(response.status_code, 400, response.content[:200])
                    self.assertIn(field, response.json())
        # batch default when only date_to precedes the arrival: a single day, zeros
        data = self.client.get(f"/api/batches/{b.pk}/stats?date_to=2026-09-01").json()
        self.assertEqual((data["period"]["date_from"], data["period"]["days"]), ("2026-09-01", 1))
        self.assertEqual(data["series"][0]["center_stock"], 0)

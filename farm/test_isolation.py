"""OWNER data isolation on every endpoint, and hardening of ids / payloads."""

from __future__ import annotations

from common.roles import Role
from common.testing import BaseAPITestCase, distribute, make_batch, make_product, make_user
from warehouse import services as warehouse_services

from . import selectors
from .models import DailyReport, PoultryMovement

HUGE_ID = 10**25


class OwnerReadIsolationTests(BaseAPITestCase):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        product = make_product()
        for household in (cls.household_a, cls.household_b):
            warehouse_services.record_stock_transaction(
                actor=cls.warehouse, product_id=product.pk, transaction_type="IN", quantity=100, document_no="K-1"
            )
            warehouse_services.record_stock_transaction(
                actor=cls.warehouse, product_id=product.pk, transaction_type="OUT", quantity=10,
                document_no="C-1", household=household,
            )

    def setUp(self):
        super().setUp()
        self.login_as(self.owner_a)

    def test_other_household_detail_and_nested_batches_are_404(self):
        for url in (f"/api/households/{self.household_b.pk}", f"/api/households/{self.household_b.pk}/batches"):
            with self.subTest(url=url):
                self.assertEqual(self.client.get(url).status_code, 404)
        self.assertEqual(self.client.get(f"/api/households/{self.household_a.pk}/batches").status_code, 200)

    def test_household_filter_cannot_widen_scope(self):
        other = self.household_b.pk
        for url in (
            f"/api/movements?household={other}",
            f"/api/daily-reports?household={other}",
            f"/api/stock-transactions?household={other}",
            f"/api/households?search={self.household_b.name}",
        ):
            with self.subTest(url=url):
                response = self.client.get(url)
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.data["count"], 0)

    def test_unfiltered_lists_contain_only_own_rows(self):
        movements = self.client.get("/api/movements").data["results"]
        self.assertTrue(movements)
        self.assertEqual({row["household"]["id"] for row in movements}, {self.household_a.pk})
        transactions = self.client.get("/api/stock-transactions").data["results"]
        self.assertEqual([(row["type"], row["household"]["id"]) for row in transactions], [("OUT", self.household_a.pk)])
        households = self.client.get("/api/households").data["results"]
        self.assertEqual([row["id"] for row in households], [self.household_a.pk])

    def test_map_contains_only_own_household(self):
        for household in (self.household_a, self.household_b):
            household.latitude, household.longitude = 39.6, 66.9
            household.save()
        rows = self.client.get("/api/households/map").data
        self.assertEqual([row["id"] for row in rows], [self.household_a.pk])

    def test_staff_endpoints_are_403(self):
        for url in (
            "/api/batches",
            f"/api/batches/{self.batch.pk}",
            "/api/products",
            "/api/users",
            f"/api/users/{self.owner_a.pk}",
            "/api/dashboard/admin",
        ):
            with self.subTest(url=url):
                self.assertEqual(self.client.get(url).status_code, 403)
        patch = self.client.patch(f"/api/households/{self.household_a.pk}", {"name": "X"}, format="json")
        self.assertEqual(patch.status_code, 403)

    def test_owner_dashboard_shows_only_own_data(self):
        data = self.client.get("/api/dashboard/owner").data
        self.assertEqual(data["household"]["id"], self.household_a.pk)
        self.assertEqual(data["bird_count"], 300)
        self.assertEqual({row["household"]["id"] for row in data["received_products"]}, {self.household_a.pk})


class OwnerWriteIsolationTests(BaseAPITestCase):
    def setUp(self):
        super().setUp()
        self.login_as(self.owner_a)

    def test_death_household_is_forced_to_own(self):
        response = self.client.post(
            "/api/movements",
            {"batch": self.batch.pk, "household": self.household_b.pk, "type": "DEATH", "quantity": 2},
            format="json",
        )
        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.data["household"]["id"], self.household_a.pk)
        self.assertEqual(selectors.household_bird_count(self.household_b.pk), 200)

    def test_death_on_batch_without_own_birds_is_rejected(self):
        other_batch = make_batch(self.admin, 100)
        distribute(self.admin, other_batch, self.household_b, 50)
        response = self.client.post(
            "/api/movements", {"batch": other_batch.pk, "type": "DEATH", "quantity": 1}, format="json"
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn("batch", response.data)
        self.assertFalse(PoultryMovement.objects.filter(batch=other_batch, type="DEATH").exists())

    def test_daily_report_household_is_forced_to_own(self):
        response = self.client.post(
            "/api/daily-reports",
            {"household": self.household_b.pk, "feed_kg": 10, "meat_kg": 5, "deaths": 1},
            format="json",
        )
        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.data["household"]["id"], self.household_a.pk)
        self.assertEqual(response.data["bird_count"], 299)
        self.assertFalse(DailyReport.objects.filter(household=self.household_b).exists())

    def test_daily_report_deaths_on_foreign_batch_roll_back(self):
        other_batch = make_batch(self.admin, 100)
        distribute(self.admin, other_batch, self.household_b, 50)
        response = self.client.post(
            "/api/daily-reports",
            {"feed_kg": 10, "meat_kg": 5, "deaths": 1, "batch": other_batch.pk},
            format="json",
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn("deaths", response.data)
        self.assertFalse(DailyReport.objects.exists())
        self.assertFalse(PoultryMovement.objects.filter(type="DEATH").exists())

    def test_owner_without_household(self):
        lonely = make_user("lonely", Role.OWNER)
        self.login_as(lonely)
        self.assertEqual(self.client.get("/api/households").data["count"], 0)
        self.assertEqual(self.client.get("/api/movements").data["count"], 0)
        self.assertEqual(self.client.get("/api/daily-reports").data["count"], 0)
        self.assertEqual(self.client.get("/api/dashboard/owner").status_code, 403)
        death = self.client.post("/api/movements", {"batch": self.batch.pk, "type": "DEATH", "quantity": 1}, format="json")
        self.assertEqual(death.status_code, 403)
        report = self.client.post("/api/daily-reports", {"feed_kg": 1, "meat_kg": 1}, format="json")
        self.assertEqual(report.status_code, 403)


class IdHardeningTests(BaseAPITestCase):
    """Out-of-range ids must never reach SQLite (``OverflowError`` → HTTP 500)."""

    def setUp(self):
        super().setUp()
        self.login_as(self.admin)

    def test_huge_query_ids_are_400(self):
        for url in (
            f"/api/movements?household={HUGE_ID}",
            f"/api/movements?batch={HUGE_ID}",
            f"/api/daily-reports?household={HUGE_ID}",
            f"/api/stock-transactions?product={HUGE_ID}",
        ):
            with self.subTest(url=url):
                self.assertEqual(self.client.get(url).status_code, 400)

    def test_huge_path_ids_are_json_404(self):
        for url in (f"/api/households/{HUGE_ID}", f"/api/households/{HUGE_ID}/batches", f"/api/batches/{HUGE_ID}"):
            with self.subTest(url=url):
                response = self.client.get(url)
                self.assertEqual(response.status_code, 404)
                self.assertEqual(response.json(), {"detail": "Topilmadi."})

    def test_huge_code_search_is_not_an_error(self):
        response = self.client.get(f"/api/households?search=XN-{HUGE_ID}")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["count"], 0)

    def test_invalid_related_ids_in_body_are_400(self):
        for value in (HUGE_ID, str(HUGE_ID), True, 1.5):
            with self.subTest(value=value):
                response = self.client.post(
                    "/api/movements", {"batch": value, "type": "OUT", "quantity": 1}, format="json"
                )
                self.assertEqual(response.status_code, 400)
                self.assertIn("batch", response.data)
        self.assertFalse(PoultryMovement.objects.filter(type="OUT").exists())

    def test_batch_quantity_is_bounded(self):
        response = self.client.post(
            "/api/batches",
            {"poultry_type": "BROILER", "breed": "X", "initial_quantity": 10**12, "age_days": 1,
             "supplier": "S", "arrived_at": "2026-01-01"},
            format="json",
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn("initial_quantity", response.data)


class HouseholdCoordinateTests(BaseAPITestCase):
    def setUp(self):
        super().setUp()
        self.login_as(self.admin)
        self.url = f"/api/households/{self.household_a.pk}"

    def test_patch_cannot_leave_half_a_coordinate(self):
        response = self.client.patch(self.url, {"latitude": 39.1}, format="json")
        self.assertEqual(response.status_code, 400)
        self.assertIn("latitude", response.data)

    def test_patch_sets_and_clears_both(self):
        response = self.client.patch(self.url, {"latitude": 39.1, "longitude": 66.9}, format="json")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.client.patch(self.url, {"latitude": 40}, format="json").status_code, 200)
        response = self.client.patch(self.url, {"latitude": None, "longitude": None}, format="json")
        self.assertEqual(response.status_code, 200)
        self.assertIsNone(response.data["latitude"])

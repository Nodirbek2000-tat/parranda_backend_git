from __future__ import annotations

from datetime import timedelta

from django.utils import timezone

from accounts.models import User
from common.roles import Role
from common.testing import BaseAPITestCase, make_user

from . import selectors
from .models import DailyReport, Household, MovementType, PoultryMovement


class HouseholdTests(BaseAPITestCase):
    payload = {
        "name": "Yangiobod 7-uy",
        "owner_first_name": "Sardor",
        "owner_last_name": "Rahimov",
        "owner_username": "sardor7",
        "phone": "+998907654321",
        "region": "Samarqand viloyati",
        "district": "Navobod tumani",
        "mfy": "Yangiobod MFY",
        "address": "7-uy",
        "latitude": 39.6543219876,
        "longitude": 66.9591,
    }

    def test_admin_creates_household_with_owner(self):
        self.login_as(self.admin)
        response = self.client.post("/api/households", self.payload, format="json")
        self.assertEqual(response.status_code, 201)
        household = response.data["household"]
        self.assertEqual(household["code"], f"XN-{household['id']:04d}")
        self.assertEqual(household["owner"]["username"], "sardor7")
        self.assertEqual(household["owner"]["full_name"], "Sardor Rahimov")
        self.assertEqual(household["bird_count"], 0)
        self.assertEqual(household["latitude"], 39.654322)
        owner = User.objects.get(username="sardor7")
        self.assertEqual(owner.role, Role.OWNER)
        self.assertTrue(owner.must_change_password)

        self.client.force_authenticate(None)
        login = self.client.post(
            "/api/auth/login",
            {"username": "sardor7", "password": response.data["temp_password"]},
            format="json",
        )
        self.assertEqual(login.status_code, 200)
        self.assertTrue(login.data["user"]["must_change_password"])
        self.assertEqual(login.data["user"]["household_id"], household["id"])

    def test_duplicate_username(self):
        self.login_as(self.admin)
        response = self.client.post("/api/households", {**self.payload, "owner_username": "owner_a"}, format="json")
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.data["owner_username"], ["Bu login band"])

    def test_super_admin_cannot_create_household(self):
        boss = make_user("boss_super", Role.ADMIN, is_superuser=True, is_staff=True)
        self.login_as(boss)
        response = self.client.post("/api/households", self.payload, format="json")
        self.assertEqual(response.status_code, 403)
        self.assertIn("faqat Administrator", str(response.data["detail"]))
        self.assertFalse(Household.objects.filter(name=self.payload["name"]).exists())

    def test_only_admin_creates_or_updates(self):
        self.login_as(self.warehouse)
        self.assertEqual(self.client.post("/api/households", self.payload, format="json").status_code, 403)
        self.assertEqual(
            self.client.patch(f"/api/households/{self.household_a.pk}", {"name": "X"}, format="json").status_code,
            403,
        )

    def test_admin_patches_location(self):
        self.login_as(self.admin)
        response = self.client.patch(
            f"/api/households/{self.household_a.pk}", {"latitude": 39.61, "longitude": 66.95}, format="json"
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["latitude"], 39.61)
        bad = self.client.patch(f"/api/households/{self.household_a.pk}", {"latitude": 120}, format="json")
        self.assertEqual(bad.status_code, 400)

    def test_bird_count_and_batches(self):
        self.login_as(self.warehouse)
        response = self.client.get(f"/api/households/{self.household_a.pk}")
        self.assertEqual(response.data["bird_count"], 300)
        batches = self.client.get(f"/api/households/{self.household_a.pk}/batches").data
        self.assertEqual(len(batches), 1)
        self.assertEqual(batches[0]["batch_code"], self.batch.code)
        self.assertEqual(batches[0]["count"], 300)
        self.assertEqual(batches[0]["poultry_type_display"], "Go‘sht yo‘nalishi")

    def test_map_lists_only_households_with_coordinates(self):
        self.household_a.latitude, self.household_a.longitude = 39.65, 66.96
        self.household_a.save()
        self.login_as(self.admin)
        response = self.client.get("/api/households/map")
        self.assertEqual(response.status_code, 200)
        self.assertEqual([row["id"] for row in response.data], [self.household_a.pk])
        self.assertEqual(response.data[0]["bird_count"], 300)
        self.assertEqual(response.data[0]["owner_name"], "Bobur Aliyev")


class OwnerScopingTests(BaseAPITestCase):
    def setUp(self):
        super().setUp()
        self.login_as(self.owner_a)

    def test_household_list_and_detail(self):
        response = self.client.get("/api/households")
        self.assertEqual([h["id"] for h in response.data["results"]], [self.household_a.pk])
        self.assertEqual(self.client.get(f"/api/households/{self.household_a.pk}").status_code, 200)
        self.assertEqual(self.client.get(f"/api/households/{self.household_b.pk}").status_code, 404)
        self.assertEqual(self.client.get(f"/api/households/{self.household_b.pk}/batches").status_code, 404)
        self.assertEqual(len(self.client.get("/api/households/map").data), 0)

    def test_movements_are_filtered(self):
        response = self.client.get("/api/movements")
        households = {m["household"]["id"] for m in response.data["results"]}
        self.assertEqual(households, {self.household_a.pk})
        response = self.client.get(f"/api/movements?household={self.household_b.pk}")
        self.assertEqual(response.data["count"], 0)

    def test_reports_are_filtered(self):
        DailyReport.objects.create(
            household=self.household_b, date=timezone.localdate(), bird_count=200, feed_kg=10, meat_kg=5
        )
        response = self.client.get("/api/daily-reports")
        self.assertEqual(response.data["count"], 0)

    def test_staff_only_endpoints(self):
        self.assertEqual(self.client.get("/api/batches").status_code, 403)
        self.assertEqual(self.client.get("/api/products").status_code, 403)
        self.assertEqual(self.client.get("/api/dashboard/admin").status_code, 403)


class BatchTests(BaseAPITestCase):
    def test_create_batch_generates_code(self):
        self.login_as(self.warehouse)
        today = timezone.localdate()
        payload = {
            "poultry_type": "BROILER",
            "breed": "Ross 308",
            "initial_quantity": 500,
            "age_days": 1,
            "supplier": "Samarqand inkubatori",
            "arrived_at": today.isoformat(),
            "initial_weight_g": 42.5,
        }
        response = self.client.post("/api/batches", payload, format="json")
        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.data["code"], f"NNP-{today.year}-{self._next_seq(today)}")
        self.assertEqual(response.data["center_stock"], 500)
        self.assertEqual(response.data["poultry_type_display"], "Go‘sht yo‘nalishi")
        self.assertEqual(response.data["health_status"], "Sog‘lom")
        self.assertEqual(response.data["initial_weight_g"], 42.5)

    def _next_seq(self, today) -> str:
        # The fixture batch arrived 30 days ago; it shares the year unless the year just changed.
        return "002" if self.batch.arrived_at.year == today.year else "001"

    def test_invalid_batch(self):
        self.login_as(self.admin)
        response = self.client.post(
            "/api/batches",
            {"poultry_type": "BROILER", "breed": "X", "initial_quantity": 0, "age_days": -1, "supplier": "Y",
             "arrived_at": (timezone.localdate() + timedelta(days=1)).isoformat()},
            format="json",
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn("initial_quantity", response.data)
        self.assertIn("age_days", response.data)

    def test_counts(self):
        self.login_as(self.admin)
        response = self.client.get(f"/api/batches/{self.batch.pk}")
        self.assertEqual(response.data["center_stock"], 500)
        self.assertEqual(response.data["distributed_total"], 500)
        self.assertEqual(response.data["deaths_total"], 0)


class MovementTests(BaseAPITestCase):
    def post(self, **payload):
        return self.client.post("/api/movements", {"batch": self.batch.pk, **payload}, format="json")

    def test_distribution_decreases_center_stock(self):
        self.login_as(self.warehouse)
        response = self.post(household=self.household_a.pk, type="DISTRIBUTE", quantity=120)
        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.data["op_id"], f"TR-{response.data['id']:06d}")
        self.assertEqual(response.data["type_display"], "Xonadonga tarqatish")
        self.assertEqual(response.data["date"], timezone.localdate().isoformat())
        self.assertEqual(selectors.batch_center_stock(self.batch), 380)
        self.assertEqual(selectors.household_bird_count(self.household_a.pk), 420)

    def test_over_distribution_is_rejected(self):
        self.login_as(self.admin)
        response = self.post(household=self.household_a.pk, type="DISTRIBUTE", quantity=501)
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.data["quantity"], ["Yetarli parranda yo‘q. Mavjud: 500"])
        self.assertEqual(selectors.batch_center_stock(self.batch), 500)

    def test_distribution_needs_active_household(self):
        self.household_a.is_active = False
        self.household_a.save()
        self.login_as(self.admin)
        response = self.post(household=self.household_a.pk, type="DISTRIBUTE", quantity=1)
        self.assertEqual(response.status_code, 400)
        self.assertIn("household", response.data)
        self.assertEqual(self.post(type="DISTRIBUTE", quantity=1).status_code, 400)

    def test_return_and_death_rules(self):
        self.login_as(self.admin)
        self.assertEqual(self.post(household=self.household_a.pk, type="RETURN", quantity=301).status_code, 400)
        self.assertEqual(self.post(household=self.household_a.pk, type="RETURN", quantity=100).status_code, 201)
        self.assertEqual(selectors.batch_center_stock(self.batch), 600)
        self.assertEqual(self.post(type="DEATH", quantity=10).status_code, 201)  # center death
        self.assertEqual(self.post(household=self.household_a.pk, type="DEATH", quantity=5).status_code, 201)
        self.assertEqual(self.post(type="OUT", quantity=591).status_code, 400)
        self.assertEqual(selectors.batch_center_stock(self.batch), 590)
        self.assertEqual(selectors.household_bird_count(self.household_a.pk), 195)
        batch = self.client.get(f"/api/batches/{self.batch.pk}").data
        self.assertEqual(batch["distributed_total"], 400)
        self.assertEqual(batch["deaths_total"], 15)

    def test_future_date_is_rejected(self):
        self.login_as(self.admin)
        tomorrow = (timezone.localdate() + timedelta(days=1)).isoformat()
        response = self.post(household=self.household_a.pk, type="DISTRIBUTE", quantity=1, date=tomorrow)
        self.assertEqual(response.status_code, 400)
        self.assertIn("date", response.data)

    def test_owner_records_only_death_in_own_household(self):
        self.login_as(self.owner_a)
        response = self.post(household=self.household_b.pk, type="DEATH", quantity=3)
        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.data["household"]["id"], self.household_a.pk)
        self.assertEqual(selectors.household_bird_count(self.household_b.pk), 200)
        self.assertEqual(self.post(type="DISTRIBUTE", quantity=1).status_code, 400)
        self.assertEqual(self.post(type="DEATH", quantity=298).status_code, 400)

    def test_list_filters_newest_first(self):
        self.login_as(self.admin)
        self.post(household=self.household_a.pk, type="DEATH", quantity=1)
        response = self.client.get(f"/api/movements?type=DEATH&household={self.household_a.pk}")
        self.assertEqual(response.data["count"], 1)
        response = self.client.get("/api/movements")
        self.assertEqual(response.data["results"][0]["type"], "DEATH")
        self.assertEqual(self.client.get("/api/movements?date_from=bad").status_code, 400)

    def test_movements_are_immutable(self):
        self.login_as(self.admin)
        movement = PoultryMovement.objects.first()
        self.assertEqual(self.client.delete(f"/api/movements/{movement.pk}").status_code, 404)
        self.assertEqual(self.client.put("/api/movements", {}, format="json").status_code, 405)


class DailyReportTests(BaseAPITestCase):
    def post(self, **payload):
        return self.client.post("/api/daily-reports", {"feed_kg": 25.5, "meat_kg": 12.75, **payload}, format="json")

    def test_owner_report_with_deaths_creates_death_movement(self):
        self.login_as(self.owner_a)
        response = self.post(deaths=3, household=self.household_b.pk)
        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.data["household"]["id"], self.household_a.pk)
        self.assertEqual(response.data["bird_count"], 297)
        self.assertEqual(response.data["feed_kg"], 25.5)
        movement = PoultryMovement.objects.get(type=MovementType.DEATH)
        self.assertEqual((movement.household_id, movement.quantity), (self.household_a.pk, 3))
        self.assertEqual(DailyReport.objects.get().death_movement, movement)

    def test_one_report_per_day(self):
        self.login_as(self.owner_a)
        self.assertEqual(self.post().status_code, 201)
        response = self.post()
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.data["date"], ["Bu sana uchun hisobot allaqachon kiritilgan"])

    def test_deaths_cannot_exceed_birds(self):
        self.login_as(self.owner_a)
        response = self.post(deaths=301)
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.data["deaths"], ["Yetarli parranda yo‘q. Mavjud: 300"])
        self.assertFalse(DailyReport.objects.exists())
        self.assertFalse(PoultryMovement.objects.filter(type=MovementType.DEATH).exists())

    def test_future_date_and_negative_values(self):
        self.login_as(self.owner_a)
        tomorrow = (timezone.localdate() + timedelta(days=1)).isoformat()
        self.assertEqual(self.post(date=tomorrow).status_code, 400)
        response = self.post(meat_kg=-1, feed_kg=-2, avg_weight_g=-5)
        self.assertEqual(response.status_code, 400)
        self.assertIn("meat_kg", response.data)
        self.assertIn("avg_weight_g", response.data)
        self.assertIn("feed_kg", response.data)

    def test_admin_must_choose_household_and_warehouse_cannot_post(self):
        self.login_as(self.admin)
        self.assertEqual(self.post().status_code, 400)
        self.assertEqual(self.post(household=self.household_b.pk).status_code, 201)
        self.login_as(self.warehouse)
        self.assertEqual(self.post(household=self.household_a.pk).status_code, 403)


class BackDatedMovementTests(BaseAPITestCase):
    """A back-dated movement is checked against every end-of-day count from its date on (V2 §1.1)."""

    def setUp(self):
        super().setUp()
        self.today = timezone.localdate()

    def day(self, offset: int) -> str:
        return (self.today + timedelta(days=offset)).isoformat()

    def post(self, **payload):
        return self.client.post("/api/movements", payload, format="json")

    def new_batch(self, quantity: int, arrived_offset: int):
        from common.testing import make_batch

        batch = make_batch(self.admin, quantity)
        type(batch).objects.filter(pk=batch.pk).update(arrived_at=self.today + timedelta(days=arrived_offset))
        batch.refresh_from_db()
        return batch

    def test_death_dated_before_the_distribution_is_rejected(self):
        self.login_as(self.warehouse)
        batch = self.new_batch(1000, -10)
        ok = self.post(batch=batch.pk, household=self.household_a.pk, type="DISTRIBUTE", quantity=100,
                       date=self.day(-2))
        self.assertEqual(ok.status_code, 201)
        # The current count (100) would allow it, but at the end of day −4 the household had none of this batch.
        response = self.post(batch=batch.pk, household=self.household_a.pk, type="DEATH", quantity=5,
                             date=self.day(-4))
        self.assertEqual(response.status_code, 400)
        limit = (self.today + timedelta(days=-4)).strftime("%d.%m.%Y")
        self.assertEqual(response.data["quantity"],
                         [f"Yetarli parranda yo‘q. Mavjud: 0 ({limit} kuni oxiridagi soni bo‘yicha)"])
        # Same day as the distribution and later: allowed.
        self.assertEqual(self.post(batch=batch.pk, household=self.household_a.pk, type="DEATH", quantity=5,
                                   date=self.day(-2)).status_code, 201)
        # A later OUT lowers the minimum of the following days: −1 has 95 − 90 = 5 left.
        self.assertEqual(self.post(batch=batch.pk, household=self.household_a.pk, type="OUT", quantity=90,
                                   date=self.day(-1)).status_code, 201)
        response = self.post(batch=batch.pk, household=self.household_a.pk, type="RETURN", quantity=6,
                             date=self.day(-2))
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.data["quantity"], ["Yetarli parranda yo‘q. Mavjud: 5"])
        self.assertEqual(self.post(batch=batch.pk, household=self.household_a.pk, type="RETURN", quantity=5,
                                   date=self.day(-2)).status_code, 201)

    def test_center_stock_never_negative_on_a_past_day(self):
        self.login_as(self.admin)
        batch = self.new_batch(100, -10)
        self.assertEqual(self.post(batch=batch.pk, household=self.household_a.pk, type="DISTRIBUTE", quantity=100,
                                   date=self.day(-3)).status_code, 201)
        self.assertEqual(self.post(batch=batch.pk, household=self.household_a.pk, type="RETURN", quantity=50,
                                   date=self.day(-1)).status_code, 201)
        # Center now has 50, but at the end of day −2 it had 0.
        response = self.post(batch=batch.pk, household=self.household_b.pk, type="DISTRIBUTE", quantity=50,
                             date=self.day(-2))
        self.assertEqual(response.status_code, 400)
        limit = (self.today + timedelta(days=-2)).strftime("%d.%m.%Y")
        self.assertEqual(response.data["quantity"],
                         [f"Yetarli parranda yo‘q. Mavjud: 0 ({limit} kuni oxiridagi soni bo‘yicha)"])
        response = self.post(batch=batch.pk, type="DEATH", quantity=1, date=self.day(-2))
        self.assertEqual(response.status_code, 400)
        self.assertEqual(self.post(batch=batch.pk, household=self.household_b.pk, type="DISTRIBUTE", quantity=50,
                                   date=self.day(-1)).status_code, 201)
        self.assertEqual(selectors.batch_center_stock(batch), 0)

    def test_back_dated_report_death_uses_a_batch_that_had_birds_that_day(self):
        batch = self.new_batch(1000, -10)
        self.login_as(self.admin)
        # Household A: 300 of the base batch since −20; 600 of the new batch only since −1 (larger today).
        self.assertEqual(self.post(batch=batch.pk, household=self.household_a.pk, type="DISTRIBUTE", quantity=600,
                                   date=self.day(-1)).status_code, 201)
        self.login_as(self.owner_a)
        response = self.client.post("/api/daily-reports", {"feed_kg": 20, "meat_kg": 10, "deaths": 2,
                                                            "date": self.day(-5)}, format="json")
        self.assertEqual(response.status_code, 201, response.data)
        movement = PoultryMovement.objects.get(type=MovementType.DEATH)
        self.assertEqual((movement.batch_id, movement.quantity), (self.batch.pk, 2))
        response = self.client.post("/api/daily-reports", {"feed_kg": 20, "meat_kg": 10, "deaths": 299,
                                                            "date": self.day(-6)}, format="json")
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.data["deaths"], ["Yetarli parranda yo‘q. Mavjud: 298"])

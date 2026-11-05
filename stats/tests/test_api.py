"""Stats endpoints end to end (API_CONTRACT_V2 §1, §5.1, §8, §10)."""

from __future__ import annotations

from datetime import date, datetime, timezone as dt_timezone
from decimal import Decimal

from django.test import override_settings

from accounts.models import User
from common.roles import Role
from common.testing import BOT_API_TOKEN, make_household, make_product, make_user
from farm.models import DailyReport, MovementType
from warehouse import services as warehouse_services
from warehouse.models import Product, ProductCategory, StockTransaction, Unit

from .helpers import FrozenClockTestCase, batch, created_on, move, report, staff, worked_example

EXAMPLE = "?date_from=2026-11-01&date_to=2026-11-03"


class WorkedExampleAPITests(FrozenClockTestCase):
    @classmethod
    def setUpTestData(cls):
        cls.admin, cls.warehouse = staff()
        data = worked_example(cls.admin)
        cls.household = data["household"]
        cls.layer = data["layer"]
        cls.broiler = data["broiler"]
        cls.owner = cls.household.owner

    def household_stats(self, query: str = EXAMPLE) -> dict:
        self.login_as(self.admin)
        response = self.client.get(f"/api/households/{self.household.pk}/stats{query}")
        self.assertEqual(response.status_code, 200, response.content)
        return response.json()

    def test_household_stats_reproduce_worked_example(self):
        data = self.household_stats()
        kpis = data["kpis"]
        self.assertEqual(kpis["laying_rate_pct"]["value"], 81.3)
        self.assertEqual(kpis["feed_per_egg_g"]["value"], 135.7)
        self.assertEqual(kpis["feed_per_bird_day_kg"]["value"], 0.110)
        self.assertEqual(kpis["mortality_pct"]["value"], 0.8)
        self.assertEqual(kpis["report_discipline_pct"]["value"], 66.7)
        self.assertEqual(kpis["birds_end"]["value"], 596)
        self.assertEqual(kpis["birds_avg"]["value"], 530.7)
        self.assertEqual(kpis["eggs"]["value"], 810)
        self.assertEqual(kpis["feed_kg"]["value"], 121.0)
        self.assertEqual(kpis["deaths"]["value"], 4)
        self.assertEqual(kpis["distributed"]["value"], 100)
        self.assertEqual(kpis["reports"]["value"], 2)
        self.assertEqual(data["missing_days"], ["2026-11-02"])
        self.assertEqual(data["discipline"], {"expected": 3, "reported": 2, "missing": 1})
        self.assertEqual(
            data["period"],
            {"date_from": "2026-11-01", "date_to": "2026-11-03", "days": 3,
             "previous_from": "2026-10-29", "previous_to": "2026-10-31"},
        )
        self.assertEqual(data["generated_at"], "2026-11-04T12:00:00+05:00")
        self.assertEqual(data["household"]["code"], self.household.code)

    def test_previous_period_and_deltas(self):
        kpis = self.household_stats()["kpis"]
        # Previous period 29.10–31.10: 500 layer birds, no reports, no deaths.
        self.assertEqual(kpis["birds_end"], {"value": 596, "previous": 500, "delta": 19.2, "delta_unit": "%"})
        self.assertEqual(kpis["eggs"], {"value": 810, "previous": 0, "delta": None, "delta_unit": "%"})
        self.assertEqual(kpis["mortality_pct"], {"value": 0.8, "previous": 0.0, "delta": 0.8, "delta_unit": "pp"})
        self.assertEqual(kpis["laying_rate_pct"]["previous"], None)
        self.assertEqual(kpis["laying_rate_pct"]["delta"], None)
        self.assertEqual(kpis["report_discipline_pct"]["previous"], 0.0)
        self.assertEqual(kpis["report_discipline_pct"]["delta"], 66.7)

    def test_series_zero_filled_oldest_first(self):
        series = self.household_stats()["series"]
        self.assertEqual([row["date"] for row in series], ["2026-11-01", "2026-11-02", "2026-11-03"])
        self.assertEqual([row["birds"] for row in series], [500, 496, 596])
        self.assertEqual([row["layer_birds"] for row in series], [500, 496, 496])
        self.assertEqual([row["eggs"] for row in series], [410, 0, 400])
        self.assertEqual([row["feed_kg"] for row in series], [55.0, 0.0, 66.0])
        self.assertEqual([row["deaths"] for row in series], [0, 4, 0])
        self.assertEqual([row["distributed"] for row in series], [0, 0, 100])
        self.assertEqual([row["reported"] for row in series], [True, False, True])
        self.assertEqual([row["expected"] for row in series], [True, True, True])
        self.assertEqual([row["laying_rate_pct"] for row in series], [82.0, None, 80.6])

    def test_by_type_and_batches(self):
        data = self.household_stats()
        self.assertEqual(
            data["by_poultry_type"],
            [
                {"poultry_type": "LAYER", "poultry_type_display": "Tuxum yo‘nalishi", "birds_end": 496,
                 "birds_avg": 497.3, "deaths": 4, "mortality_pct": 0.8},
                {"poultry_type": "BROILER", "poultry_type_display": "Go‘sht yo‘nalishi", "birds_end": 100,
                 "birds_avg": 33.3, "deaths": 0, "mortality_pct": 0.0},
            ],
        )
        self.assertEqual([(row["batch_id"], row["count"]) for row in data["batches"]],
                         [(self.layer.pk, 496), (self.broiler.pk, 100)])

    def test_my_stats_is_identical_for_the_owner(self):
        expected = self.household_stats()
        self.login_as(self.owner)
        response = self.client.get(f"/api/my/stats{EXAMPLE}")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), expected)
        own = self.client.get(f"/api/households/{self.household.pk}/stats{EXAMPLE}")
        self.assertEqual(own.json(), expected)

    def test_overview_matches_single_household(self):
        self.login_as(self.warehouse)
        data = self.client.get(f"/api/stats/overview{EXAMPLE}").json()
        self.assertEqual(data["kpis"], self.household_stats()["kpis"])
        self.assertEqual(data["households"], {"total": 1, "active": 1, "with_birds": 1, "reporting": 1})
        # Center: layer 1000 − 500 = 500, broiler 300 − 100 = 200 (arrived 01.11 ≤ 03.11).
        self.assertEqual(data["center"], {"birds_end": 700, "deaths": 0, "out": 0})
        self.assertEqual([row["expected_reports"] for row in data["series"]], [1, 1, 1])
        self.assertEqual([row["reports"] for row in data["series"]], [1, 0, 1])
        self.assertEqual([row["poultry_type"] for row in data["by_poultry_type"]], ["LAYER", "BROILER", "BREEDER"])
        self.assertEqual(
            data["by_poultry_type"][2],
            {"poultry_type": "BREEDER", "poultry_type_display": "Naslli", "households": 0, "birds_end": 0,
             "birds_avg": 0.0, "deaths": 0, "distributed": 0, "mortality_pct": None},
        )
        self.assertEqual(data["by_poultry_type"][1]["distributed"], 100)
        district = data["by_district"][0]
        self.assertEqual(
            {key: district[key] for key in ("households", "birds_end", "eggs", "laying_rate_pct", "mortality_pct",
                                            "report_discipline_pct")},
            {"households": 1, "birds_end": 596, "eggs": 810, "laying_rate_pct": 81.3, "mortality_pct": 0.8,
             "report_discipline_pct": 66.7},
        )
        self.assertEqual(data["top_households"], [])  # only 2 reports (< 3)
        self.assertEqual(data["high_mortality"], {"threshold_pct": 5.0, "count": 0, "items": []})

    def test_overview_center_counts_only_arrived_batches(self):
        self.login_as(self.admin)
        data = self.client.get("/api/stats/overview?date_from=2026-10-20&date_to=2026-10-25").json()
        self.assertEqual(data["center"]["birds_end"], 500)  # broiler batch arrives on 01.11
        threshold = self.client.get(f"/api/stats/overview{EXAMPLE}&mortality_threshold=0.5").json()
        self.assertEqual(threshold["high_mortality"]["count"], 1)
        self.assertEqual(threshold["high_mortality"]["items"][0]["mortality_pct"], 0.8)
        self.assertEqual(threshold["high_mortality"]["items"][0]["birds_avg"], 530.7)
        strict = self.client.get(f"/api/stats/overview{EXAMPLE}&mortality_threshold=0.8").json()
        self.assertEqual(strict["high_mortality"]["count"], 0)  # 0.7538… is not > 0.8

    def test_report_bird_count_snapshot_is_ignored(self):
        before = self.household_stats()
        DailyReport.objects.filter(household=self.household).update(bird_count=9999)
        self.assertEqual(self.household_stats(), before)

    def test_backdated_movement_changes_past_days(self):
        move(self.admin, self.layer, self.household, MovementType.DEATH, 10, date(2026, 11, 1))
        data = self.household_stats()
        self.assertEqual([row["birds"] for row in data["series"]], [490, 486, 586])
        self.assertEqual(data["kpis"]["deaths"]["value"], 14)
        # 810 eggs / (490 + 486) layer bird-days.
        self.assertEqual(data["kpis"]["laying_rate_pct"]["value"], 83.0)

    def test_inactive_household(self):
        self.household.is_active = False
        self.household.save()
        data = self.household_stats()
        self.assertEqual(data["discipline"], {"expected": 0, "reported": 0, "missing": 0})
        self.assertEqual(data["missing_days"], [])
        self.assertIsNone(data["kpis"]["report_discipline_pct"]["value"])
        self.assertEqual(data["kpis"]["eggs"]["value"], 810)

    def test_household_created_after_period_start(self):
        created_on(self.household, date(2026, 11, 2))
        data = self.household_stats()
        self.assertEqual(data["discipline"], {"expected": 2, "reported": 1, "missing": 1})
        self.assertEqual([row["expected"] for row in data["series"]], [False, True, True])

    def test_batch_stats(self):
        move(self.admin, self.layer, None, MovementType.DEATH, 3, date(2026, 11, 2))
        move(self.admin, self.layer, None, MovementType.OUT, 7, date(2026, 11, 3))
        move(self.admin, self.layer, self.household, MovementType.RETURN, 6, date(2026, 11, 3))
        move(self.admin, self.layer, self.household, MovementType.OUT, 5, date(2026, 11, 4))
        self.login_as(self.warehouse)
        data = self.client.get(f"/api/batches/{self.layer.pk}/stats").json()
        totals = data["totals"]
        self.assertEqual(
            totals,
            {"initial_quantity": 1000, "center_stock": 496, "in_households": 485, "alive": 981, "distributed": 500,
             "returned": 6, "deaths_total": 7, "deaths_center": 3, "deaths_households": 4, "out_total": 12,
             "out_center": 7, "out_households": 5, "households_now": 1, "mortality_pct": 0.7},
        )
        self.assertEqual(
            totals["initial_quantity"],
            totals["center_stock"] + totals["in_households"] + totals["deaths_total"] + totals["out_total"],
        )
        self.assertEqual(data["batch"]["center_stock"], 496)
        # Default period: from arrival (15.10) to today (04.11).
        self.assertEqual(data["period"], {"date_from": "2026-10-15", "date_to": "2026-11-04", "days": 21})
        self.assertEqual(data["age"], {"age_days_at_arrival": 1, "days_since_arrival": 20, "age_days_today": 21})
        self.assertEqual(len(data["series"]), 21)
        first, last = data["series"][0], data["series"][-1]
        self.assertEqual((first["center_stock"], first["in_households"]), (1000, 0))
        self.assertEqual((last["center_stock"], last["in_households"]), (496, 485))
        by_day = {row["date"]: row for row in data["series"]}
        self.assertEqual(by_day["2026-10-20"]["distributed"], 500)
        self.assertEqual(by_day["2026-11-02"]["deaths"], 7)  # center 3 + household 4
        self.assertEqual(by_day["2026-11-03"]["returned"], 6)
        self.assertEqual(
            data["households"],
            [{"household": {"id": self.household.pk, "code": self.household.code, "name": "Navbahor 12-uy"},
              "is_active": True, "count_now": 485, "distributed": 500, "returned": 6, "deaths": 4, "out": 5,
              "mortality_pct": 0.8}],
        )
        explicit = self.client.get(f"/api/batches/{self.layer.pk}/stats?date_from=2026-10-10&date_to=2026-10-16").json()
        self.assertEqual([row["center_stock"] for row in explicit["series"]], [0, 0, 0, 0, 0, 1000, 1000])
        self.assertNotIn("previous_from", explicit["period"])


class ProductStatsTests(FrozenClockTestCase):
    @classmethod
    def setUpTestData(cls):
        cls.admin, cls.warehouse = staff()
        cls.household = make_household("Navbahor 12-uy", "owner_p")
        cls.feed = make_product("Yem PK-1", min_stock="500")
        # Older than every default period (N_eff = N): the §1.3 example has a 30-day-old product.
        Product.objects.filter(pk=cls.feed.pk).update(created_at=datetime(2026, 9, 1, 4, 0, tzinfo=dt_timezone.utc))

    def tx(self, kind: str, quantity: str, household=None, when: datetime | None = None):
        row = warehouse_services.record_stock_transaction(
            actor=self.warehouse, product_id=self.feed.pk, transaction_type=kind, quantity=Decimal(quantity),
            document_no="D-1", household=household,
        )
        if when is not None:
            StockTransaction.objects.filter(pk=row.pk).update(created_at=when)
        return row

    def test_days_of_cover_example(self):
        self.tx("IN", "2700")
        self.tx("OUT", "600", self.household)
        self.tx("OUT", "600")
        self.tx("OUT", "300", self.household)
        self.login_as(self.admin)
        data = self.client.get(f"/api/products/{self.feed.pk}/stats").json()
        self.assertEqual(data["product"]["balance"], 1200.0)
        self.assertEqual(data["avg_daily_out"], 50.0)
        self.assertEqual(data["days_of_cover"], 24.0)
        self.assertEqual(data["stockout_date"], "2026-11-28")
        self.assertEqual(
            data["totals"],
            {"opening_balance": 0.0, "in_qty": 2700.0, "in_count": 1, "out_qty": 1500.0, "out_count": 3,
             "net": 1200.0, "adjust_in_qty": 0.0, "adjust_out_qty": 0.0, "closing_balance": 1200.0},
        )
        self.assertEqual(data["top_consumers"][0]["quantity"], 900.0)
        self.assertEqual(data["top_consumers"][0]["share_pct"], 60.0)
        self.assertEqual(data["top_consumers"][0]["transactions"], 2)
        self.assertEqual(data["out_unassigned_qty"], 600.0)
        self.assertEqual(len(data["series"]), 30)
        self.assertEqual(data["series"][-1], {"date": "2026-11-04", "in_qty": 2700.0, "out_qty": 1500.0,
                                              "adjust_qty": 0.0, "balance": 1200.0})

    def test_opening_and_closing_balance_and_timezone_boundary(self):
        self.tx("IN", "100.25", when=datetime(2026, 10, 1, 8, 0, tzinfo=dt_timezone.utc))
        self.tx("OUT", "40.10", when=datetime(2026, 11, 2, 18, 59, tzinfo=dt_timezone.utc))  # 02.11 23:59 local
        last = self.tx("IN", "10.05", when=datetime(2026, 11, 3, 19, 30, tzinfo=dt_timezone.utc))  # 04.11 00:30
        self.login_as(self.warehouse)
        data = self.client.get(f"/api/products/{self.feed.pk}/stats?date_from=2026-11-02&date_to=2026-11-04").json()
        self.assertEqual(data["totals"]["opening_balance"], 100.25)
        self.assertEqual(data["totals"]["closing_balance"], float(last.balance_after))
        self.assertEqual(data["totals"]["closing_balance"], 70.2)
        self.assertEqual(
            data["series"],
            [{"date": "2026-11-02", "in_qty": 0.0, "out_qty": 40.1, "adjust_qty": 0.0, "balance": 60.15},
             {"date": "2026-11-03", "in_qty": 0.0, "out_qty": 0.0, "adjust_qty": 0.0, "balance": 60.15},
             {"date": "2026-11-04", "in_qty": 10.05, "out_qty": 0.0, "adjust_qty": 0.0, "balance": 70.2}],
        )
        self.assertEqual(data["avg_daily_out"], 13.367)
        self.assertEqual(data["days_of_cover"], 5.3)  # 70.20 / 13.3666…
        self.assertEqual(data["stockout_date"], "2026-11-09")

    def test_new_product_uses_effective_days(self):
        """Created today: IN 1 350, OUT 150 → 150 kg/day (N_eff = 1), not 150 / 30 = 5 kg/day."""
        fresh = make_product("Yem yangi", min_stock="100")
        for kind, quantity in (("IN", "1350"), ("OUT", "150")):
            warehouse_services.record_stock_transaction(
                actor=self.warehouse, product_id=fresh.pk, transaction_type=kind, quantity=Decimal(quantity),
                document_no="D-2",
            )
        self.login_as(self.admin)
        data = self.client.get(f"/api/products/{fresh.pk}/stats").json()
        self.assertEqual((data["avg_daily_out"], data["days_of_cover"], data["stockout_date"]),
                         (150.0, 8.0, "2026-11-12"))
        # Created 3 days ago (02.11), period 30 days: N_eff = 3 → 50 kg/day, cover 24.0.
        Product.objects.filter(pk=fresh.pk).update(created_at=datetime(2026, 11, 2, 4, 0, tzinfo=dt_timezone.utc))
        data = self.client.get(f"/api/products/{fresh.pk}/stats").json()
        self.assertEqual((data["avg_daily_out"], data["days_of_cover"]), (50.0, 24.0))
        # A period that starts after the product appeared keeps N.
        data = self.client.get(f"/api/products/{fresh.pk}/stats?date_from=2026-11-03").json()
        self.assertEqual(data["avg_daily_out"], 75.0)
        warehouse = self.client.get("/api/warehouse/stats").json()
        item = next(row for row in warehouse["items"] if row["id"] == fresh.pk)
        self.assertEqual(item["avg_daily_out"], 50.0)
        # FEED/kg group: earliest product (Yem PK-1, 01.09) → N_eff = 30 → 150 / 30.
        self.assertEqual(warehouse["feed"]["avg_daily_out_kg"], 5.0)

    def test_no_out_means_no_cover(self):
        self.tx("IN", "10")
        self.login_as(self.admin)
        data = self.client.get(f"/api/products/{self.feed.pk}/stats").json()
        self.assertIsNone(data["days_of_cover"])
        self.assertIsNone(data["stockout_date"])
        self.assertEqual(data["avg_daily_out"], 0.0)
        self.assertEqual(data["top_consumers"], [])

    def test_inactive_product_works(self):
        Product.objects.filter(pk=self.feed.pk).update(is_active=False)
        self.login_as(self.admin)
        self.assertEqual(self.client.get(f"/api/products/{self.feed.pk}/stats").status_code, 200)

    def test_warehouse_stats(self):
        self.tx("IN", "2700")
        self.tx("OUT", "1500", self.household)
        vaccine = Product.objects.create(
            name="Vaksina ND", category=ProductCategory.VACCINE, unit=Unit.PIECE, min_stock=Decimal("100")
        )
        warehouse_services.record_stock_transaction(
            actor=self.warehouse, product_id=vaccine.pk, transaction_type="IN", quantity=Decimal("100"),
            document_no="V-1",
        )
        warehouse_services.record_stock_transaction(
            actor=self.warehouse, product_id=vaccine.pk, transaction_type="OUT", quantity=Decimal("60"),
            document_no="V-2",
        )
        Product.objects.create(name="Eski yem", category=ProductCategory.FEED, unit=Unit.KG, is_active=False)
        Product.objects.create(name="Qop yem", category=ProductCategory.FEED, unit=Unit.SACK)
        Product.objects.update(created_at=datetime(2026, 9, 1, 4, 0, tzinfo=dt_timezone.utc))
        StockTransaction.objects.update(created_at=datetime(2026, 11, 4, 4, 0, tzinfo=dt_timezone.utc))
        self.login_as(self.warehouse)
        data = self.client.get("/api/warehouse/stats").json()
        self.assertEqual(data["products"], {"total": 4, "active": 3, "low": 2})
        self.assertEqual(
            data["feed"],
            {"balance_kg": 1200.0, "in_kg": 2700.0, "out_kg": 1500.0, "avg_daily_out_kg": 50.0,
             "days_of_cover": 24.0, "stockout_date": "2026-11-28"},
        )
        self.assertEqual([item["name"] for item in data["items"]], ["Qop yem", "Vaksina ND", "Yem PK-1"])
        self.assertEqual(
            [(row["category"], row["unit"], row["products"]) for row in data["by_category"]],
            [("FEED", "kg", 1), ("FEED", "qop", 1), ("VACCINE", "dona", 1)],
        )
        # Low stock: vaccine (40 left, 2 per day → 20 days) before "Qop yem" (no OUT → null cover last).
        self.assertEqual([row["name"] for row in data["low_stock"]], ["Vaksina ND", "Qop yem"])
        self.assertEqual(data["low_stock"][0]["days_of_cover"], 20.0)
        self.assertEqual(data["low_stock"][0]["stockout_date"], "2026-11-24")
        self.assertEqual(data["top_consumers"][0]["feed_kg"], 1500.0)
        self.assertEqual(data["top_consumers"][0]["share_pct"], 100.0)
        self.assertEqual(data["series"][-1], {"date": "2026-11-04", "in_count": 2, "out_count": 2,
                                              "feed_in_kg": 2700.0, "feed_out_kg": 1500.0})
        self.assertNotIn("previous_from", data["period"])


class RankingTests(FrozenClockTestCase):
    @classmethod
    def setUpTestData(cls):
        cls.admin, cls.warehouse = staff()
        layer = batch(cls.admin, 10_000, date(2026, 10, 1))
        cls.households = []
        # eggs per day for 100 layer birds → laying rate 50 … 90 %; household 7 has only 2 reports.
        for index, eggs in enumerate([50, 90, 70, 90, 60, 80, 85]):
            household = make_household(f"Uy {index}", f"rank_owner{index}")
            created_on(household, date(2026, 10, 1))
            move(cls.admin, layer, household, MovementType.DISTRIBUTE, 100, date(2026, 10, 2))
            days = 2 if index == 6 else 3 + (index == 3)
            for offset in range(days):
                report(household, date(2026, 11, 1 + offset), eggs, "10")
            cls.households.append(household)
        cls.households[4].is_active = False
        cls.households[4].save()
        move(cls.admin, layer, cls.households[0], MovementType.DEATH, 9, date(2026, 11, 2))
        move(cls.admin, layer, cls.households[2], MovementType.DEATH, 1, date(2026, 11, 2))

    def test_top_bottom_and_high_mortality(self):
        self.login_as(self.admin)
        data = self.client.get("/api/stats/overview?date_from=2026-11-01&date_to=2026-11-04").json()
        ids = [h.pk for h in self.households]
        top = [row["household"]["id"] for row in data["top_households"]]
        # 90 % twice: household 3 has more eggs (4 reports) than household 1, then 80 %, 70 %, 60 %.
        self.assertEqual(top, [ids[3], ids[1], ids[5], ids[2], ids[4]])
        self.assertEqual([row["household"]["id"] for row in data["bottom_households"]], [ids[0]])
        first = data["top_households"][0]
        self.assertEqual((first["laying_rate_pct"], first["eggs"], first["reports"], first["layer_birds_avg"]),
                         (90.0, 360, 4, 100.0))
        bottom = data["bottom_households"][0]
        self.assertEqual(bottom["laying_rate_pct"], 53.2)  # 150 eggs / (100 + 91 + 91) layer bird-days
        self.assertEqual(data["households"], {"total": 7, "active": 6, "with_birds": 7, "reporting": 7})
        high = data["high_mortality"]
        self.assertEqual(high["count"], 1)
        self.assertEqual(high["items"][0]["household"]["id"], ids[0])
        self.assertEqual(high["items"][0]["deaths"], 9)
        self.assertEqual(high["items"][0]["mortality_pct"], 9.7)  # 100 × 9 / ((100 + 91×3) / 4) = 9.651…
        low = self.client.get("/api/stats/overview?date_from=2026-11-01&date_to=2026-11-04&mortality_threshold=0").json()
        self.assertEqual([row["household"]["id"] for row in low["high_mortality"]["items"]], [ids[0], ids[2]])

    def test_district_grouping(self):
        Household = type(self.households[0])
        Household.objects.filter(pk=self.households[0].pk).update(district="Urgut tumani")
        self.login_as(self.admin)
        rows = self.client.get("/api/stats/overview").json()["by_district"]
        self.assertEqual([(row["district"], row["households"]) for row in rows],
                         [("Navobod tumani", 6), ("Urgut tumani", 1)])
        self.assertEqual(rows[0]["households_active"], 5)


class EmptyDataTests(FrozenClockTestCase):
    @classmethod
    def setUpTestData(cls):
        cls.admin, cls.warehouse = staff()

    def test_no_data_never_fails(self):
        self.login_as(self.admin)
        overview = self.client.get("/api/stats/overview").json()
        self.assertEqual(len(overview["series"]), 30)
        self.assertEqual(overview["kpis"]["eggs"], {"value": 0, "previous": 0, "delta": None, "delta_unit": "%"})
        self.assertIsNone(overview["kpis"]["laying_rate_pct"]["value"])
        self.assertEqual(overview["by_district"], [])
        self.assertEqual(overview["center"], {"birds_end": 0, "deaths": 0, "out": 0})
        warehouse = self.client.get("/api/warehouse/stats?date_from=2000-01-01&date_to=2000-12-31").json()
        self.assertEqual(warehouse["items"], [])
        self.assertEqual(len(warehouse["series"]), 366)
        self.assertIsNone(warehouse["feed"]["days_of_cover"])

    def test_period_errors_reach_the_client(self):
        self.login_as(self.admin)
        cases = {
            "?date_from=2026-13-01": {"date_from": ["Sana noto‘g‘ri formatda (YYYY-MM-DD)."]},
            "?date_to=2026-11-05": {"date_to": ["Sana kelajakda bo‘lishi mumkin emas"]},
            "?date_from=2026-11-04&date_to=2026-11-01": {
                "date_from": ["Boshlanish sanasi tugash sanasidan keyin bo‘lishi mumkin emas."]},
            "?date_from=1999-01-01&date_to=1999-06-01": {"date_from": ["Sana 01.01.2000 dan oldin bo‘lishi mumkin emas."]},
            "?date_from=2024-01-01": {"non_field_errors": ["Davr 366 kundan oshmasligi kerak."]},
            "?mortality_threshold=101": {"mortality_threshold": ["0 dan 100 gacha son kiriting."]},
            "?mortality_threshold=abc": {"mortality_threshold": ["0 dan 100 gacha son kiriting."]},
            # Exponent notation would make Fraction(threshold) build 10**999999999 (CPU / memory DoS).
            "?mortality_threshold=1e-999999999": {"mortality_threshold": ["0 dan 100 gacha son kiriting."]},
            "?mortality_threshold=1e-999999999999999999": {"mortality_threshold": ["0 dan 100 gacha son kiriting."]},
            # At most one decimal: the echoed threshold_pct (1 dec) is exactly the value compared against.
            "?mortality_threshold=4.96": {"mortality_threshold": ["0 dan 100 gacha son kiriting."]},
        }
        for query, expected in cases.items():
            with self.subTest(query=query):
                response = self.client.get(f"/api/stats/overview{query}")
                self.assertEqual(response.status_code, 400)
                self.assertEqual(response.json(), expected)
        self.assertEqual(self.client.get("/api/warehouse/stats?date_to=2026-11-05").status_code, 400)
        self.assertEqual(self.client.get("/api/stats/overview?unknown=1").status_code, 200)


class RoleMatrixTests(FrozenClockTestCase):
    @classmethod
    def setUpTestData(cls):
        cls.admin, cls.warehouse = staff()
        cls.household_a = make_household("A uy", "owner_ra")
        cls.household_b = make_household("B uy", "owner_rb")
        cls.owner_a = cls.household_a.owner
        cls.owner_no_household = make_user("owner_nohh", Role.OWNER)
        cls.batch = batch(cls.admin, 100, date(2026, 11, 1))
        cls.product = make_product()

    def status(self, user, method: str, url: str, data=None) -> int:
        self.client.force_authenticate(user=user)
        return getattr(self.client, method)(url, data, format="json").status_code

    def test_matrix(self):
        a, w, o = self.admin, self.warehouse, self.owner_a
        hid, other = self.household_a.pk, self.household_b.pk
        cases = [
            ("get", "/api/stats/overview", {a: 200, w: 200, o: 403, None: 401}),
            ("get", "/api/warehouse/stats", {a: 200, w: 200, o: 403, None: 401}),
            ("get", f"/api/households/{hid}/stats", {a: 200, w: 200, o: 200, None: 401}),
            ("get", f"/api/households/{other}/stats", {a: 200, w: 200, o: 404}),
            ("get", "/api/households/999999/stats", {a: 404, o: 404}),
            ("get", f"/api/households/{10**25}/stats", {a: 404}),
            ("get", "/api/my/stats", {a: 403, w: 403, o: 200, self.owner_no_household: 403, None: 401}),
            ("get", f"/api/batches/{self.batch.pk}/stats", {a: 200, w: 200, o: 403, None: 401}),
            ("get", "/api/batches/999999/stats", {a: 404}),
            ("get", f"/api/products/{self.product.pk}/stats", {a: 200, w: 200, o: 403, None: 401}),
            ("get", "/api/products/999999/stats", {w: 404}),
            ("post", "/api/stats/overview", {a: 405}),
            ("post", "/api/my/stats", {o: 405}),
            ("post", f"/api/households/{hid}/stats", {a: 405}),
            ("delete", "/api/warehouse/stats", {a: 405}),
        ]
        for method, url, expected in cases:
            for user, code in expected.items():
                with self.subTest(method=method, url=url, user=getattr(user, "username", None)):
                    self.assertEqual(self.status(user, method, url), code)

    def test_owner_without_household_message(self):
        self.client.force_authenticate(user=self.owner_no_household)
        response = self.client.get("/api/my/stats")
        self.assertEqual(response.json(), {"detail": "Sizga xonadon biriktirilmagan. Administratorga murojaat qiling."})


@override_settings(BOT_API_TOKEN=BOT_API_TOKEN)
class BotSummaryTests(FrozenClockTestCase):
    @classmethod
    def setUpTestData(cls):
        cls.admin, cls.warehouse = staff()
        data = worked_example(cls.admin)
        cls.household = data["household"]
        cls.layer = data["layer"]
        User.objects.filter(pk=cls.household.owner_id).update(telegram_id=5001, must_change_password=False)
        User.objects.filter(pk=cls.admin.pk).update(telegram_id=5002)
        User.objects.filter(pk=cls.warehouse.pk).update(telegram_id=5003)
        report(cls.household, date(2026, 11, 4), 395, "60.50", deaths=2)
        move(cls.admin, cls.layer, None, MovementType.DEATH, 1, date(2026, 11, 4))
        cls.other = make_household("Bo‘sh uy", "owner_empty")
        make_product("Yem PK-1", min_stock="100")  # balance 0 ≤ 100 → low

    def summary(self, telegram_id: int, token: str | None = BOT_API_TOKEN):
        headers = {"HTTP_X_BOT_TOKEN": token} if token else {}
        return self.client.get(f"/api/bot/users/{telegram_id}/summary", **headers)

    def test_owner(self):
        data = self.summary(5001).json()
        self.assertEqual(data["role"], "OWNER")
        self.assertEqual(data["date"], "2026-11-04")
        self.assertEqual(data["household"], {"id": self.household.pk, "code": self.household.code,
                                             "name": "Navbahor 12-uy"})
        self.assertEqual(data["bird_count"], 594)
        self.assertEqual(data["today"], {"report_submitted": True, "eggs": 395, "feed_kg": 60.5, "meat_kg": None,
                                         "deaths": 2})
        week = data["last_7d"]
        self.assertEqual((week["date_from"], week["date_to"], week["days"]), ("2026-10-29", "2026-11-04", 7))
        self.assertEqual((week["reports"], week["reported"], week["expected"], week["eggs"], week["feed_kg"],
                          week["deaths"]), (3, 3, 7, 1205, 181.5, 6))
        # (410 + 400 + 395) / (500 + 496 + 494) layer bird-days
        self.assertEqual(week["laying_rate_pct"], 80.9)

    def test_owner_without_report_today(self):
        DailyReport.objects.filter(date=date(2026, 11, 4)).delete()
        data = self.summary(5001).json()
        self.assertEqual(data["today"], {"report_submitted": False, "eggs": None, "feed_kg": None, "meat_kg": None,
                                         "deaths": 2})
        # Today is not over: without a report it is not (yet) an expected day.
        week = data["last_7d"]
        self.assertEqual((week["reports"], week["reported"], week["expected"]), (2, 2, 6))

    def test_staff(self):
        data = self.summary(5003).json()
        self.assertEqual(data["role"], "WAREHOUSE")
        self.assertEqual(
            data["today"],
            # "Bo‘sh uy" is active but has no birds: not due today (same rule as series[today]).
            {"households_active": 2, "households_expected": 1, "households_reported": 1,
             "report_discipline_pct": 100.0, "eggs": 395, "feed_kg": 60.5, "meat_kg": 0.0, "laying_rate_pct": 80.0,
             "deaths": 2,
             "deaths_center": 1, "birds_in_households": 594, "birds_in_center": 699, "birds_total": 1293,
             "low_stock_count": 1},
        )
        self.assertEqual(self.summary(5002).json()["role"], "ADMIN")

    def test_staff_today_equals_overview_series_today(self):
        """Bot and web show the same numbers for the same day (finding: 8 / 11 vs 8 / 10)."""
        today = self.summary(5002).json()["today"]
        self.login_as(self.admin)
        day = self.client.get("/api/stats/overview?date_from=2026-11-04").json()["series"][-1]
        self.assertEqual(
            (today["households_reported"], today["households_expected"], today["eggs"], today["feed_kg"],
             today["deaths"], today["laying_rate_pct"]),
            (day["reported_expected"], day["expected_reports"], day["eggs"], day["feed_kg"], day["deaths"],
             day["laying_rate_pct"]),
        )

    def test_owner_week_equals_web_discipline(self):
        week = self.summary(5001).json()["last_7d"]
        self.login_as(self.admin)
        web = self.client.get(f"/api/households/{self.household.pk}/stats?date_from=2026-10-29").json()
        self.assertEqual((week["reported"], week["expected"]),
                         (web["discipline"]["reported"], web["discipline"]["expected"]))

    def test_errors(self):
        self.assertEqual(self.summary(5001, token="wrong").status_code, 403)
        self.assertEqual(self.summary(5001, token=None).status_code, 403)
        missing = self.summary(424242)
        self.assertEqual((missing.status_code, missing.json()), (404, {"detail": "Foydalanuvchi topilmadi"}))
        User.objects.filter(pk=self.warehouse.pk).update(is_active=False)
        blocked = self.summary(5003)
        self.assertEqual(blocked.status_code, 403)
        self.assertEqual(blocked.json(), {"detail": "Hisobingiz bloklangan. Administratorga murojaat qiling."})
        make_user("owner_lonely", Role.OWNER, telegram_id=5009)
        lonely = self.summary(5009)
        self.assertEqual(lonely.status_code, 403)
        self.assertEqual(lonely.json(), {"detail": "Sizga xonadon biriktirilmagan. Administratorga murojaat qiling."})
        self.assertEqual(self.client.post("/api/bot/users/5001/summary", HTTP_X_BOT_TOKEN=BOT_API_TOKEN).status_code,
                         405)


class LargePeriodTests(FrozenClockTestCase):
    @classmethod
    def setUpTestData(cls):
        cls.admin, cls.warehouse = staff()
        data = worked_example(cls.admin)
        cls.household = data["household"]

    def test_full_year_and_series_length(self):
        self.login_as(self.admin)
        for query, days in (("", 30), ("?date_from=2025-11-04", 366), ("?date_from=2026-11-04", 1)):
            with self.subTest(query=query):
                data = self.client.get(f"/api/households/{self.household.pk}/stats{query}").json()
                self.assertEqual(len(data["series"]), days)
                self.assertEqual(data["series"][-1]["date"], "2026-11-04")
                dates = [row["date"] for row in data["series"]]
                self.assertEqual(dates, sorted(dates))


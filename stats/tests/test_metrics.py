"""Pure unit tests of the §1.2 formulas, the bird-count reconstruction and the period rules (no database)."""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from fractions import Fraction

from django.test import SimpleTestCase
from rest_framework.exceptions import ValidationError

from stats import metrics
from stats.flock import FlockWindow, HouseholdMeta, ScopeStats, Step
from stats.metrics import Totals
from stats.periods import Period, parse_threshold, resolve_period
from stats.queries import NO_WEIGHT


def indexed(start: date, rows) -> list[tuple[int, int, int, int, int]]:
    """Report rows as returned by ``queries.report_rows``: the date becomes a day index from ``start``.

    Input rows: ``(household_id, day, meat_cents, feed_cents[, weight_cents])`` (no weight → ``NO_WEIGHT``).
    """
    result = []
    for household_id, day, meat, feed, *weight in rows:
        result.append((household_id, (day - start).days, meat, feed, weight[0] if weight else NO_WEIGHT))
    return result

D = Decimal
TODAY = date(2026, 11, 4)


class RoundingTests(SimpleTestCase):
    def test_half_up_not_bankers(self):
        self.assertEqual(metrics.round_half_up(D("0.05"), 1), 0.1)
        self.assertEqual(metrics.round_half_up(D("0.15"), 1), 0.2)
        self.assertEqual(metrics.round_half_up(D("0.25"), 1), 0.3)  # banker's rounding would give 0.2
        self.assertEqual(metrics.round_half_up(D("2.345"), 2), 2.35)
        self.assertEqual(metrics.round_half_up(D("0.1105"), 3), 0.111)
        self.assertEqual(metrics.round_half_up(D("-1.25"), 1), -1.3)  # half away from zero

    def test_none_and_negative_zero(self):
        self.assertIsNone(metrics.round_half_up(None, 1))
        self.assertEqual(str(metrics.round_half_up(D("-0.04"), 1)), "0.0")
        self.assertEqual(str(metrics.round_half_up(D("-0"), 2)), "0.0")

    def test_fraction_input(self):
        self.assertEqual(metrics.round_half_up(Fraction(2, 3) * 100, 1), 66.7)


class FormulaTests(SimpleTestCase):
    def test_division_by_zero_is_none(self):
        self.assertIsNone(metrics.ratio(5, 0))
        self.assertIsNone(metrics.fcr(5500, 0))
        self.assertIsNone(metrics.meat_per_bird_kg(2550, 0, 30))
        self.assertIsNone(metrics.meat_per_bird_kg(2550, 500, 0))
        self.assertIsNone(metrics.avg_weight_g(180000, 0))
        self.assertIsNone(metrics.feed_per_bird_day_kg(100, 0))
        self.assertIsNone(metrics.mortality_pct(3, 0, 30))
        self.assertIsNone(metrics.discipline_pct(0, 0))
        self.assertIsNone(metrics.birds_avg(0, 0))
        self.assertEqual(metrics.birds_avg(0, 30), 0)
        self.assertIsNone(metrics.days_of_cover(D(10), D(0)))
        self.assertIsNone(metrics.days_of_cover(D(10), None))
        self.assertIsNone(metrics.share_pct(1, 0))

    def test_meat_and_feed(self):
        # FCR: 121 kg feed / 66 kg meat = 1.8333… → 1.83 (lower is better).
        self.assertEqual(metrics.fcr(12100, 6600).quantize(D("0.0001")), D("1.8333"))
        self.assertEqual(metrics.round_half_up(metrics.fcr(12100, 6600), 2), 1.83)
        self.assertEqual(metrics.round_half_up(metrics.fcr(5500, 2550), 2), 2.16)  # 2.1568…
        self.assertEqual(metrics.round_half_up(metrics.fcr(6600, 4050), 2), 1.63)  # 1.6296…
        # Meat per average bird: 66 kg / (1592 bird-days / 3 days = 530.667 birds) = 0.12437… kg.
        self.assertEqual(metrics.meat_per_bird_kg(6600, 1592, 3).quantize(D("0.000001")), D("0.124372"))
        self.assertEqual(metrics.round_half_up(metrics.meat_per_bird_kg(6600, 1592, 3), 3), 0.124)
        # Bird-weighted weight: (1800 g × 500 + 2000 g × 596) / 1096 = 1908.759… g.
        weighted = 180000 * 500 + 200000 * 596
        self.assertEqual(metrics.avg_weight_g(weighted, 1096).quantize(D("0.001")), D("1908.759"))
        self.assertEqual(metrics.round_half_up(metrics.avg_weight_g(weighted, 1096), 1), 1908.8)
        self.assertEqual(metrics.round_half_up(metrics.feed_per_bird_day_kg(12100, 1096), 3), 0.110)

    def test_mortality_uses_average_flock(self):
        value = metrics.mortality_pct(4, 500 + 496 + 596, 3)
        self.assertEqual(value.quantize(D("0.0001")), D("0.7538"))
        self.assertEqual(metrics.round_half_up(value, 1), 0.8)
        self.assertEqual(metrics.mortality_fraction(4, 1592, 3), Fraction(1200, 1592))
        # Not capped at 100 %.
        self.assertEqual(metrics.round_half_up(metrics.mortality_pct(30, 20, 1), 1), 150.0)

    def test_discipline(self):
        self.assertEqual(metrics.round_half_up(metrics.discipline_pct(2, 3), 1), 66.7)

    def test_stock_cover_example(self):
        daily = metrics.avg_daily_out(D(1500), 30)
        self.assertEqual(metrics.round_half_up(daily, 3), 50.0)
        cover = metrics.days_of_cover(D(1200), daily)
        self.assertEqual(metrics.round_half_up(cover, 1), 24.0)
        self.assertEqual(metrics.stockout_date(TODAY, cover), date(2026, 11, 28))

    def test_stockout_floor_and_cap(self):
        self.assertEqual(metrics.stockout_date(TODAY, D("23.96")), date(2026, 11, 27))
        self.assertEqual(metrics.stockout_date(TODAY, D("0.4")), TODAY)
        self.assertEqual(metrics.stockout_date(TODAY, D(3650)), date(2036, 11, 1))
        self.assertIsNone(metrics.stockout_date(TODAY, D("3650.01")))
        self.assertIsNone(metrics.stockout_date(TODAY, None))


class DeltaTests(SimpleTestCase):
    def test_relative_delta_example(self):
        self.assertEqual(metrics.round_half_up(metrics.delta_relative(61200, 58000), 1), 5.5)
        self.assertEqual(metrics.round_half_up(metrics.delta_relative(14, 20), 1), -30.0)

    def test_points_delta_uses_unrounded_values(self):
        # 81.325 vs 79.04 → 2.285 → 2.3 (rounded values 81.3 − 79.0 would give 2.3 too, but 81.34 − 79.06 ≠).
        self.assertEqual(metrics.round_half_up(metrics.delta_points(D("81.325"), D("79.04")), 1), 2.3)
        self.assertEqual(metrics.round_half_up(metrics.delta_points(D("81.34"), D("79.06")), 1), 2.3)
        self.assertEqual(metrics.round_half_up(metrics.delta_points(D("10.04"), D("10.0")), 1), 0.0)

    def test_null_rules(self):
        self.assertIsNone(metrics.delta_relative(5, 0))
        self.assertIsNone(metrics.delta_relative(5, None))
        self.assertIsNone(metrics.delta_relative(None, 5))
        self.assertIsNone(metrics.delta_points(None, D(5)))
        self.assertIsNone(metrics.delta_points(D(5), None))
        self.assertEqual(metrics.delta_points(D(0), D(0)), 0)

    def test_kpi_object(self):
        self.assertEqual(
            metrics.kpi(61200, 58000, places=None, unit="%"),
            {"value": 61200, "previous": 58000, "delta": 5.5, "delta_unit": "%"},
        )
        self.assertEqual(
            metrics.kpi(D("81.325"), D("79.04"), places=1, unit="pp"),
            {"value": 81.3, "previous": 79.0, "delta": 2.3, "delta_unit": "pp"},
        )
        self.assertEqual(
            metrics.kpi(3000, 0, places=None, unit="%"),
            {"value": 3000, "previous": 0, "delta": None, "delta_unit": "%"},
        )

    def test_kpis_of_empty_totals(self):
        result = metrics.kpis(Totals(), Totals(), 30)
        self.assertEqual(list(result), list(metrics.KPI_SPEC))
        self.assertEqual(len(result), 14)
        self.assertEqual(result["meat_kg"], {"value": 0.0, "previous": 0.0, "delta": None, "delta_unit": "%"})
        self.assertEqual(result["birds_avg"]["value"], 0.0)
        self.assertEqual(result["feed_kg"]["value"], 0.0)
        for key in ("fcr", "meat_per_bird_kg", "avg_weight_g", "feed_per_bird_day_kg", "mortality_pct",
                    "report_discipline_pct"):
            self.assertEqual(result[key], {"value": None, "previous": None, "delta": None,
                                           "delta_unit": metrics.KPI_SPEC[key][1]})


class StepTests(SimpleTestCase):
    def test_value_sum_and_positive_ranges(self):
        step = Step(10, {0: 5, 3: -15, 6: 7})  # 15 15 15 0 0 0 7 7 ...
        self.assertEqual([step.at(i) for i in range(8)], [15, 15, 15, 0, 0, 0, 7, 7])
        self.assertEqual(step.total(0, 7), 15 * 3 + 7 * 2)
        self.assertEqual(step.total(2, 4), 15)
        self.assertEqual(step.total(5, 4), 0)
        self.assertEqual(step.positive_ranges(1, 9), [(1, 2), (6, 9)])
        self.assertEqual(step.positive_days(0, 9), 7)

    def test_zero_changes_are_ignored(self):
        step = Step(3, {2: 0})
        self.assertEqual(step.starts, [0])
        self.assertEqual(step.total(0, 9), 30)


def worked_example_scope(previous: bool = False) -> ScopeStats:
    """§1.3: XN-0003, period 01.11–03.11.2026 (in-memory rows, exactly what the queries return)."""
    start = date(2026, 11, 1)
    end = date(2026, 11, 3)
    opening = [(3, "BREEDER", 500, -500)]  # DISTRIBUTE 500 on 20.10 (breeder batch)
    daily = [
        # household, type, date, household_net, center_net, deaths, distributed, returned, out
        (3, "BREEDER", date(2026, 11, 2), -4, 0, 4, 0, 0, 0),
        (3, "BROILER", date(2026, 11, 3), 100, -100, 0, 100, 0, 0),
    ]
    # household, date, meat (hundredths of kg), feed (hundredths of kg), avg weight (hundredths of g)
    reports = [(3, date(2026, 11, 1), 2550, 5500, 180000), (3, date(2026, 11, 3), 4050, 6600, 200000)]
    meta = HouseholdMeta(3, "Navbahor 12-uy", "Samarqand viloyati", "Navobod tumani", True, date(2026, 10, 1))
    window = FlockWindow(start, end, opening, daily)
    return ScopeStats(window, [meta], indexed(start, reports), current=(0, 2))


class WorkedExampleTests(SimpleTestCase):
    def test_bird_counts_by_day(self):
        scope = worked_example_scope()
        state = scope.states[3]
        self.assertEqual([state.all.at(i) for i in range(3)], [500, 496, 596])
        self.assertEqual([state.by_type["BREEDER"].at(i) for i in range(3)], [500, 496, 496])
        self.assertEqual(scope.series.birds, [500, 496, 596])
        self.assertEqual(scope.series.meat_cents, [2550, 0, 4050])
        self.assertEqual(scope.series.feed_cents, [5500, 0, 6600])
        self.assertEqual(scope.series.weight_bird_cents, [180000 * 500, 0, 200000 * 596])
        self.assertEqual(scope.series.weighed_bird_days, [500, 0, 596])
        self.assertEqual(scope.series.expected, [1, 1, 1])
        self.assertEqual(scope.reported_days, {0, 2})

    def test_exact_numbers(self):
        scope = worked_example_scope()
        totals = scope.total_current()
        self.assertEqual(totals.bird_days, 1592)
        self.assertEqual(totals.meat_cents, 6600)
        self.assertEqual(totals.feed_cents, 12100)
        self.assertEqual(totals.weight_bird_cents, 209_200_000)
        self.assertEqual(totals.weighed_bird_days, 1096)
        self.assertEqual(totals.rp_feed_cents, 12100)
        self.assertEqual(totals.rp_bird_days, 1096)
        self.assertEqual((totals.expected, totals.reported_expected), (3, 2))
        values = metrics.kpi_values(totals, 3)
        self.assertEqual(metrics.round_half_up(values["fcr"], 2), 1.83)
        self.assertEqual(metrics.round_half_up(values["meat_per_bird_kg"], 3), 0.124)
        self.assertEqual(metrics.round_half_up(values["avg_weight_g"], 1), 1908.8)
        self.assertEqual(metrics.round_half_up(values["feed_per_bird_day_kg"], 3), 0.110)
        self.assertEqual(metrics.round_half_up(values["birds_avg"], 3), 530.667)
        self.assertEqual(metrics.round_half_up(values["mortality_pct"], 1), 0.8)
        self.assertEqual(metrics.round_half_up(values["report_discipline_pct"], 1), 66.7)
        self.assertEqual(values["meat_kg"], D("66"))
        self.assertEqual(values["feed_kg"], D("121"))
        self.assertEqual(values["deaths"], 4)
        self.assertEqual(values["distributed"], 100)

    def test_inactive_household_is_not_expected(self):
        start, end = date(2026, 11, 1), date(2026, 11, 3)
        meta = HouseholdMeta(3, "X", "R", "D", False, date(2026, 10, 1))
        window = FlockWindow(start, end, [(3, "BROILER", 500, -500)], [])
        scope = ScopeStats(window, [meta], indexed(start, [(3, start, 400, 100)]), current=(0, 2))
        totals = scope.total_current()
        self.assertEqual((totals.expected, totals.reported_expected), (0, 0))
        self.assertEqual(totals.reports, 1)

    def test_not_expected_before_creation_or_without_birds(self):
        start, end = date(2026, 11, 1), date(2026, 11, 5)
        meta = HouseholdMeta(1, "X", "R", "D", True, date(2026, 11, 2))
        # 10 birds until 03.11 (all returned on 04.11).
        window = FlockWindow(start, end, [(1, "BROILER", 10, -10)],
                             [(1, "BROILER", date(2026, 11, 4), -10, 10, 0, 0, 10, 0)])
        scope = ScopeStats(window, [meta], [], current=(0, 4))
        self.assertEqual(scope.series.expected, [0, 1, 1, 0, 0])
        self.assertEqual(scope.series.birds, [10, 10, 10, 0, 0])

    def test_report_without_meat_or_weight(self):
        """Feed without meat → FCR null; a report without weight (or without birds) is not in the weight mean."""
        start = date(2026, 11, 1)
        meta = HouseholdMeta(1, "X", "R", "D", True, date(2026, 10, 1))
        window = FlockWindow(start, start, [(1, "BROILER", 50, -50)], [])
        scope = ScopeStats(window, [meta], indexed(start, [(1, start, 0, 900)]), current=(0, 0))
        totals = scope.total_current()
        values = metrics.kpi_values(totals, 1)
        self.assertEqual(totals.meat_cents, 0)
        self.assertIsNone(values["fcr"])
        self.assertIsNone(values["avg_weight_g"])
        self.assertEqual(values["meat_per_bird_kg"], 0)
        self.assertEqual(metrics.round_half_up(values["feed_per_bird_day_kg"], 3), 0.18)

    def test_weight_mean_skips_days_without_birds(self):
        # 40 birds on 01.11, all returned on 02.11: the 02.11 report (weight 2500 g) has B = 0 and is skipped.
        start, end = date(2026, 11, 1), date(2026, 11, 2)
        meta = HouseholdMeta(1, "X", "R", "D", True, date(2026, 10, 1))
        window = FlockWindow(start, end, [(1, "BROILER", 40, -40)],
                             [(1, "BROILER", date(2026, 11, 2), -40, 40, 0, 0, 40, 0)])
        rows = [(1, start, 1000, 2000, 150000), (1, end, 500, 800, 250000)]
        scope = ScopeStats(window, [meta], indexed(start, rows), current=(0, 1))
        totals = scope.total_current()
        values = metrics.kpi_values(totals, 2)
        self.assertEqual(metrics.round_half_up(values["avg_weight_g"], 1), 1500.0)
        self.assertEqual(metrics.round_half_up(values["fcr"], 2), 1.87)  # 28 kg / 15 kg = 1.8666…
        self.assertEqual(metrics.round_half_up(values["meat_per_bird_kg"], 3), 0.75)  # 15 kg / (40 / 2) birds
        self.assertEqual(values["meat_kg"], D(15))

    def test_previous_period_is_split(self):
        start = date(2026, 10, 29)  # previous 29.10–31.10, current 01.11–03.11
        meta = HouseholdMeta(3, "X", "R", "D", True, date(2026, 10, 1))
        window = FlockWindow(start, date(2026, 11, 3), [(3, "BROILER", 500, -500)], [])
        reports = [(3, date(2026, 10, 30), 300, 100), (3, date(2026, 11, 2), 450, 100)]
        scope = ScopeStats(window, [meta], indexed(start, reports), current=(3, 5), previous=(0, 2))
        self.assertEqual(scope.total_current().meat_cents, 450)
        self.assertEqual(scope.total_previous().meat_cents, 300)
        self.assertEqual(scope.total_previous().birds_end, 500)
        self.assertEqual(scope.total_previous().expected, 3)


class PeriodTests(SimpleTestCase):
    def errors(self, params) -> dict:
        with self.assertRaises(ValidationError) as ctx:
            resolve_period(params, TODAY)
        return ctx.exception.detail

    def test_defaults(self):
        period = resolve_period({}, TODAY)
        self.assertEqual((period.date_from, period.date_to, period.days), (date(2026, 10, 6), TODAY, 30))
        self.assertEqual((period.previous_from, period.previous_to), (date(2026, 9, 6), date(2026, 10, 5)))
        self.assertEqual(resolve_period({"date_from": "2026-11-01"}, TODAY).date_to, TODAY)
        only_to = resolve_period({"date_to": "2026-10-31"}, TODAY)
        self.assertEqual((only_to.date_from, only_to.days), (date(2026, 10, 2), 30))
        self.assertEqual(resolve_period({"date_from": "", "date_to": " "}, TODAY), period)

    def test_as_dict_and_dates(self):
        period = Period(date(2026, 11, 1), date(2026, 11, 3))
        self.assertEqual(
            period.as_dict(),
            {"date_from": "2026-11-01", "date_to": "2026-11-03", "days": 3,
             "previous_from": "2026-10-29", "previous_to": "2026-10-31"},
        )
        self.assertNotIn("previous_from", period.as_dict(previous=False))
        self.assertEqual(period.dates(), [date(2026, 11, 1), date(2026, 11, 2), date(2026, 11, 3)])

    def test_bad_format(self):
        for raw in ("2026-1-5", "05.11.2026", "2026-02-30", "abc", "2026-11-04T00:00"):
            with self.subTest(raw=raw):
                self.assertEqual(self.errors({"date_from": raw}), {"date_from": ["Sana noto‘g‘ri formatda (YYYY-MM-DD)."]})
        self.assertEqual(
            set(self.errors({"date_from": "x", "date_to": "y"})), {"date_from", "date_to"}
        )

    def test_order_of_checks(self):
        self.assertEqual(self.errors({"date_to": "2026-11-05"}), {"date_to": ["Sana kelajakda bo‘lishi mumkin emas"]})
        self.assertEqual(
            self.errors({"date_from": "2026-11-03", "date_to": "2026-11-01"}),
            {"date_from": ["Boshlanish sanasi tugash sanasidan keyin bo‘lishi mumkin emas."]},
        )
        self.assertEqual(
            self.errors({"date_from": "1999-12-31", "date_to": "2000-01-05"}),
            {"date_from": ["Sana 01.01.2000 dan oldin bo‘lishi mumkin emas."]},
        )
        self.assertEqual(
            self.errors({"date_from": "2025-11-03", "date_to": "2026-11-04"}),
            {"non_field_errors": ["Davr 366 kundan oshmasligi kerak."]},
        )
        self.assertEqual(resolve_period({"date_from": "2025-11-04", "date_to": "2026-11-04"}, TODAY).days, 366)
        both = self.errors({"date_from": "2026-11-06", "date_to": "2026-11-05"})
        self.assertEqual(set(both), {"date_from", "date_to"})

    def test_custom_default_start(self):
        period = resolve_period({}, TODAY, default_from=lambda date_to: date(2026, 11, 1))
        self.assertEqual((period.date_from, period.days), (date(2026, 11, 1), 4))

    def test_threshold(self):
        self.assertEqual(parse_threshold({}), D(5))
        self.assertEqual(parse_threshold({"mortality_threshold": "2.5"}), D("2.5"))
        self.assertEqual(parse_threshold({"mortality_threshold": "0"}), D(0))
        self.assertEqual(parse_threshold({"mortality_threshold": "100"}), D(100))
        self.assertEqual(parse_threshold({"mortality_threshold": "4.9"}), D("4.9"))
        self.assertEqual(parse_threshold({"mortality_threshold": " 7 "}), D(7))
        for raw in ("-1", "100.1", "abc", "NaN", "Infinity", "1e-999999999", "1e-999999999999999999", "5e0",
                    "4.96", "2.25", "0.05", "1000", "5.", ".5", "+5", "5,5", "0x10", "١٢"):
            with self.subTest(raw=raw), self.assertRaises(ValidationError) as ctx:
                parse_threshold({"mortality_threshold": raw})
            self.assertEqual(ctx.exception.detail, {"mortality_threshold": ["0 dan 100 gacha son kiriting."]})

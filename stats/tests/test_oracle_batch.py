"""Brute-force oracle for batch stats and the bot summary (verification of API_CONTRACT_V2 §1.6, §5.1)."""

from __future__ import annotations

from datetime import date, timedelta
from fractions import Fraction

from django.db.models import F
from django.test import override_settings

from common.testing import BOT_API_TOKEN
from farm.models import MovementType, PoultryBatch, PoultryMovement
from warehouse.models import Product

from .helpers import TODAY, FrozenClockTestCase
from .test_oracle import Oracle, build_fixture, days_of, div, pct, rhu


def batch_oracle(b, date_from, date_to):
    moves = list(PoultryMovement.objects.filter(batch=b))
    far = date(9999, 1, 1)

    def center_at(d):
        if d < b.arrived_at:
            return 0
        c = b.initial_quantity
        for m in moves:
            if m.date > d:
                continue
            if m.type == "DISTRIBUTE":
                c -= m.quantity
            elif m.type == "RETURN":
                c += m.quantity
            elif m.household_id is None:
                c -= m.quantity
        return c

    def house_at(d, h=None):
        return sum(
            (m.quantity if m.type == "DISTRIBUTE" else -m.quantity)
            for m in moves
            if m.household_id is not None and m.date <= d and (h is None or m.household_id == h)
        )

    def flow(kind, d=None, where=None):
        return sum(
            m.quantity
            for m in moves
            if m.type == kind
            and (d is None or m.date == d)
            and (where is None or (m.household_id is None) == (where == "center"))
        )

    rows = []
    for h in sorted({m.household_id for m in moves if m.household_id is not None}):
        mine = [m for m in moves if m.household_id == h]
        dist = sum(m.quantity for m in mine if m.type == "DISTRIBUTE")
        deaths = sum(m.quantity for m in mine if m.type == "DEATH")
        rows.append({
            "id": h,
            "count_now": house_at(far, h),
            "distributed": dist,
            "returned": sum(m.quantity for m in mine if m.type == "RETURN"),
            "deaths": deaths,
            "out": sum(m.quantity for m in mine if m.type == "OUT"),
            "mortality_pct": rhu(pct(deaths, dist), 1),
        })
    rows.sort(key=lambda r: (-r["count_now"], r["id"]))
    deaths_total = flow("DEATH")
    totals = {
        "initial_quantity": b.initial_quantity,
        "center_stock": center_at(far),
        "in_households": house_at(far),
        "alive": center_at(far) + house_at(far),
        "distributed": flow("DISTRIBUTE"),
        "returned": flow("RETURN"),
        "deaths_total": deaths_total,
        "deaths_center": flow("DEATH", where="center"),
        "deaths_households": flow("DEATH", where="house"),
        "out_total": flow("OUT"),
        "out_center": flow("OUT", where="center"),
        "out_households": flow("OUT", where="house"),
        "households_now": sum(1 for r in rows if r["count_now"] > 0),
        "mortality_pct": rhu(pct(deaths_total, b.initial_quantity), 1),
    }
    series = [
        {
            "date": d.isoformat(),
            "center_stock": center_at(d),
            "in_households": house_at(d),
            "distributed": flow("DISTRIBUTE", d),
            "returned": flow("RETURN", d),
            "deaths": flow("DEATH", d),
            "out": flow("OUT", d),
        }
        for d in days_of(date_from, date_to)
    ]
    return totals, rows, series


class BatchSummaryOracleMixin:
    SEED = 4
    PERIODS = [
        (TODAY - timedelta(days=29), TODAY),
        (TODAY - timedelta(days=150), TODAY - timedelta(days=100)),
        (TODAY - timedelta(days=365), TODAY),
    ]

    @classmethod
    def setUpTestData(cls):
        cls.admin, cls.households, cls.batches, cls.products = build_fixture(cls.SEED)

    def get(self, url, **headers):
        self.login_as(self.admin)
        response = self.client.get(url, **headers)
        self.assertEqual(response.status_code, 200, response.content[:500])
        return response.json()

    def test_batch_stats_match_oracle(self):
        for b in PoultryBatch.objects.all():
            periods = [*self.PERIODS, (b.arrived_at - timedelta(days=3), b.arrived_at + timedelta(days=3))]
            for date_from, date_to in periods:
                with self.subTest(batch=b.code, period=(date_from, date_to)):
                    data = self.get(f"/api/batches/{b.pk}/stats?date_from={date_from}&date_to={date_to}")
                    totals, rows, series = batch_oracle(b, date_from, date_to)
                    self.assertEqual(data["totals"], totals)
                    t = data["totals"]
                    self.assertEqual(
                        t["initial_quantity"], t["center_stock"] + t["in_households"] + t["deaths_total"] + t["out_total"]
                    )
                    got_rows = [
                        {"id": r["household"]["id"],
                         **{k: r[k] for k in ("count_now", "distributed", "returned", "deaths", "out", "mortality_pct")}}
                        for r in data["households"]
                    ]
                    self.assertEqual(got_rows, rows)
                    self.assertEqual(data["series"], series)
            with self.subTest(batch=b.code, period="default"):
                data = self.get(f"/api/batches/{b.pk}/stats")
                self.assertEqual(
                    data["period"]["date_from"], max(b.arrived_at, TODAY - timedelta(days=365)).isoformat()
                )
                self.assertEqual(data["period"]["date_to"], TODAY.isoformat())
                self.assertEqual(data["age"]["days_since_arrival"], (TODAY - b.arrived_at).days)
                self.assertEqual(data["age"]["age_days_today"], b.age_days + (TODAY - b.arrived_at).days)

    @override_settings(BOT_API_TOKEN=BOT_API_TOKEN)
    def test_bot_summary_matches_oracle(self):
        oracle = Oracle()
        week = days_of(TODAY - timedelta(days=6), TODAY)
        for i, h in enumerate(self.households):
            owner = h.owner
            owner.telegram_id = 900000 + i + 100 * self.SEED
            owner.must_change_password = False
            owner.save(update_fields=["telegram_id", "must_change_password"])
            data = self.get(f"/api/bot/users/{owner.telegram_id}/summary", HTTP_X_BOT_TOKEN=BOT_API_TOKEN)
            rep = oracle.reports.get((h.pk, TODAY))
            v = oracle.values([h.pk], week)
            with self.subTest(household=h.pk):
                self.assertEqual(data["bird_count"], oracle.B(h.pk, TODAY))
                self.assertEqual(data["today"], {
                    "report_submitted": rep is not None,
                    "feed_kg": rhu(rep[1], 2) if rep else None,
                    "meat_kg": rhu(oracle.meat_by[(h.pk, TODAY)], 2) if rep and (h.pk, TODAY) in oracle.meat_by
                    else None,
                    "avg_weight_g": rhu(rep[2], 1) if rep else None,
                    "deaths": oracle.flows([h.pk], [TODAY], MovementType.DEATH),
                })
                self.assertEqual(data["last_7d"]["meat_kg"],
                                 rhu(sum((oracle.meat_by.get((h.pk, d), 0) for d in week), Fraction(0)), 2))
                last = data["last_7d"]
                self.assertEqual(last["reports"], v["reports"])
                self.assertEqual(last["expected"], v["_expected"])
                self.assertEqual(last["reported"], v["_reported_expected"])
                self.assertEqual(last["feed_kg"], rhu(v["feed_kg"], 2))
                self.assertEqual(last["meat_kg"], rhu(v["meat_kg"], 2))
                self.assertEqual(last["deaths"], v["deaths"])
                self.assertEqual(last["fcr"], rhu(v["fcr"], 2))
                self.assertEqual(last["avg_weight_g"], rhu(v["avg_weight_g"], 1))
        self.admin.telegram_id = 777000 + self.SEED
        self.admin.save(update_fields=["telegram_id"])
        data = self.get(f"/api/bot/users/{self.admin.telegram_id}/summary", HTTP_X_BOT_TOKEN=BOT_API_TOKEN)["today"]
        H = sorted(oracle.households)
        active = [h for h in H if oracle.households[h].is_active]
        reps = [(h, oracle.reports[(h, TODAY)]) for h in H if (h, TODAY) in oracle.reports]
        due = [h for h in active if oracle.expected(h, TODAY)]
        reported_due = sum(1 for h, _ in reps if h in due)
        in_house = sum(oracle.B(h, TODAY) for h in H)
        in_center = oracle.overview(TODAY, TODAY)["center"]["birds_end"]
        self.assertEqual(data, {
            "households_active": len(active),
            "households_expected": len(due),
            "households_reported": reported_due,
            "report_discipline_pct": rhu(pct(reported_due, len(due)), 1),
            "feed_kg": rhu(sum((f for _, (_m, f, _w) in reps), Fraction(0)), 2),
            "meat_kg": rhu(oracle.meat.get(TODAY, Fraction(0)), 2),
            "fcr": rhu(div(sum((f for _, (_m, f, _w) in reps), Fraction(0)),
                           sum((m for _, (m, _f, _w) in reps), Fraction(0))), 2),
            "deaths": sum(q for h, _t, k, q, d, _b in oracle.moves if k == "DEATH" and d == TODAY and h is not None),
            "deaths_center": sum(q for h, _t, k, q, d, _b in oracle.moves if k == "DEATH" and d == TODAY and h is None),
            "birds_in_households": in_house,
            "birds_in_center": in_center,
            "birds_total": in_house + in_center,
            "low_stock_count": Product.objects.filter(is_active=True, balance__lte=F("min_stock")).count(),
        })


class BatchSummaryOracleSeed4Tests(BatchSummaryOracleMixin, FrozenClockTestCase):
    SEED = 4


class BatchSummaryOracleSeed5Tests(BatchSummaryOracleMixin, FrozenClockTestCase):
    SEED = 5

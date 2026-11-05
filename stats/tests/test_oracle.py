"""Independent brute-force oracle for every stats endpoint (verification of API_CONTRACT_V2 §1).

The oracle below re-implements the contract formulas in the most literal way possible — a Python loop
over every calendar day and every household, exact ``Fraction`` arithmetic, half-up rounding of the
exact value — directly from the raw rows in the database. It shares no code with ``stats.flock`` /
``stats.metrics`` / ``stats.services``. Randomised but seeded data (several seeds) exercise back-dated
movements, mixed flocks, inactive households, households created during the period, centre deaths,
stock transactions on the local-midnight boundaries and inactive products.
"""

from __future__ import annotations

import random
from collections import defaultdict
from datetime import date, datetime, time, timedelta, timezone as dt_timezone
from decimal import Decimal
from fractions import Fraction
from math import floor
from unittest import mock

from django.utils import timezone
from rest_framework.exceptions import ValidationError

from common.testing import make_household, make_user
from common.roles import Role
from farm import services as farm_services
from farm.models import DailyReport, Household, MovementType, PoultryBatch, PoultryMovement, PoultryType
from warehouse import services as warehouse_services
from warehouse.models import Product, ProductCategory, StockTransaction, Unit

from .helpers import TASHKENT, TODAY, FrozenClockTestCase, local_dt

TYPES = (PoultryType.BROILER, PoultryType.BREEDER)
CATEGORY_ORDER = list(ProductCategory.values)
UNIT_ORDER = list(Unit.values)


# --- exact helpers ---------------------------------------------------------------------------


def rhu(value, places):
    """Half-up (ties away from zero) rounding of an exact value, as a float; ``None`` stays ``None``."""
    if value is None:
        return None
    value = Fraction(value)
    scaled = value * 10**places
    sign = -1 if scaled < 0 else 1
    rounded = sign * floor(abs(scaled) + Fraction(1, 2))
    result = float(Fraction(rounded, 10**places))
    return 0.0 if result == 0 else result


def div(a, b):
    return None if not b else Fraction(a) / Fraction(b)


def pct(a, b):
    value = div(a, b)
    return None if value is None else 100 * value


def days_of(date_from: date, date_to: date) -> list[date]:
    return [date_from + timedelta(days=i) for i in range((date_to - date_from).days + 1)]


def local_day(dt: datetime) -> date:
    return dt.astimezone(TASHKENT).date()


# --- the oracle ------------------------------------------------------------------------------


class Oracle:
    def __init__(self):
        self.households = {h.pk: h for h in Household.objects.all()}
        self.moves = [
            (m.household_id, m.batch.poultry_type, m.type, m.quantity, m.date, m.batch_id)
            for m in PoultryMovement.objects.select_related("batch")
        ]
        # (meat kg — 0 when not stated, feed kg, average weight g or None)
        self.reports = {
            (r.household_id, r.date): (
                Fraction(r.meat_kg) if r.meat_kg is not None else Fraction(0),
                Fraction(r.feed_kg),
                Fraction(r.avg_weight_g) if r.avg_weight_g is not None else None,
            )
            for r in DailyReport.objects.all()
        }
        self.meat = defaultdict(Fraction)
        self.meat_by: dict = {}
        for r in DailyReport.objects.filter(meat_kg__isnull=False):
            self.meat[r.date] += Fraction(r.meat_kg)
            self.meat_by[(r.household_id, r.date)] = Fraction(r.meat_kg)
        self.batches = list(PoultryBatch.objects.all())

    # bird counts -------------------------------------------------------------------------
    def B(self, h, d, t=None):
        total = 0
        for hid, ptype, kind, qty, mdate, _batch in self.moves:
            if hid != h or mdate > d or (t is not None and ptype != t):
                continue
            total += qty if kind == MovementType.DISTRIBUTE else -qty
        return total

    def expected(self, h, d):
        hh = self.households[h]
        return hh.is_active and d >= local_day(hh.created_at) and self.B(h, d) > 0

    def flows(self, H, dates, kind, t=None):
        dates = set(dates)
        return sum(
            qty
            for hid, ptype, k, qty, mdate, _b in self.moves
            if hid in H and k == kind and mdate in dates and (t is None or ptype == t)
        )

    # KPI values (exact) ------------------------------------------------------------------
    def values(self, H, dates):
        N = len(dates)
        bird_days = sum(self.B(h, d) for h in H for d in dates)
        meat = feed = Fraction(0)
        reports = 0
        weight_b = Fraction(0)
        weighed_b = 0
        rp_feed = Fraction(0)
        rp_b = 0
        expected = reported_expected = 0
        for h in H:
            for d in dates:
                exp = self.expected(h, d)
                rep = self.reports.get((h, d))
                # today is not over: it is an expected day of the period only once reported
                expected += exp and (d < TODAY or rep is not None)
                if rep is None:
                    continue
                m, f, w = rep
                meat += m
                feed += f
                reports += 1
                if exp:
                    reported_expected += 1
                b = self.B(h, d)
                if b > 0 and w is not None:
                    weight_b += w * b
                    weighed_b += b
                if b > 0:
                    rp_feed += f
                    rp_b += b
        birds_avg = Fraction(bird_days, N)
        deaths = self.flows(H, dates, MovementType.DEATH)
        return {
            "birds_end": sum(self.B(h, dates[-1]) for h in H),
            "birds_avg": birds_avg,
            "meat_kg": meat,
            "feed_kg": feed,
            "deaths": deaths,
            "distributed": self.flows(H, dates, MovementType.DISTRIBUTE),
            "returned": self.flows(H, dates, MovementType.RETURN),
            "reports": reports,
            "fcr": div(feed, meat),
            "meat_per_bird_kg": None if not birds_avg else meat / birds_avg,
            "avg_weight_g": div(weight_b, weighed_b),
            "feed_per_bird_day_kg": div(rp_feed, rp_b),
            "mortality_pct": None if not birds_avg else 100 * deaths / birds_avg,
            "report_discipline_pct": pct(reported_expected, expected),
            "_expected": expected,
            "_reported_expected": reported_expected,
        }

    SPEC = {
        "birds_end": (None, "%"), "birds_avg": (1, "%"), "meat_kg": (2, "%"), "feed_kg": (2, "%"),
        "deaths": (None, "%"), "distributed": (None, "%"), "returned": (None, "%"), "reports": (None, "%"),
        "fcr": (2, "%"), "meat_per_bird_kg": (3, "%"), "avg_weight_g": (1, "%"), "feed_per_bird_day_kg": (3, "%"),
        "mortality_pct": (1, "pp"), "report_discipline_pct": (1, "pp"),
    }

    def kpis(self, H, dates, prev_dates):
        now, before = self.values(H, dates), self.values(H, prev_dates)
        out = {}
        for key, (places, unit) in self.SPEC.items():
            v, p = now[key], before[key]
            if unit == "pp":
                delta = None if v is None or p is None else v - p
            else:
                delta = None if v is None or p is None or p == 0 else 100 * (Fraction(v) - Fraction(p)) / Fraction(p)

            def fmt(x):
                if x is None:
                    return None
                return int(x) if places is None else rhu(x, places)

            out[key] = {"value": fmt(v), "previous": fmt(p), "delta": rhu(delta, 1), "delta_unit": unit}
        return out

    def day_series(self, H, d):
        reps = [(h, self.reports[(h, d)]) for h in H if (h, d) in self.reports]
        meat = sum((m for _, (m, _f, _w) in reps), Fraction(0))
        feed = sum((f for _, (_m, f, _w) in reps), Fraction(0))
        weighed = [(w, self.B(h, d)) for h, (_m, _f, w) in reps if w is not None and self.B(h, d) > 0]
        return {
            "date": d.isoformat(),
            "birds": sum(self.B(h, d) for h in H),
            "meat_kg": rhu(meat, 2),
            "feed_kg": rhu(feed, 2),
            "avg_weight_g": rhu(div(sum((w * b for w, b in weighed), Fraction(0)), sum(b for _, b in weighed)), 1),
            "fcr": rhu(div(feed, meat), 2),
            "deaths": self.flows(H, [d], MovementType.DEATH),
            "distributed": self.flows(H, [d], MovementType.DISTRIBUTE),
            "returned": self.flows(H, [d], MovementType.RETURN),
        }, len(reps)

    def type_row(self, H, dates, t):
        N = len(dates)
        bird_days = sum(self.B(h, d, t) for h in H for d in dates)
        deaths = self.flows(H, dates, MovementType.DEATH, t)
        avg = Fraction(bird_days, N)
        return {
            "poultry_type": t,
            "birds_end": sum(self.B(h, dates[-1], t) for h in H),
            "birds_avg": rhu(avg, 1),
            "deaths": deaths,
            "mortality_pct": rhu(None if not avg else 100 * deaths / avg, 1),
            "_households": sum(1 for h in H if self.B(h, dates[-1], t) > 0),
            "_distributed": self.flows(H, dates, MovementType.DISTRIBUTE, t),
        }

    # endpoints -----------------------------------------------------------------------------
    def household(self, h, date_from, date_to):
        dates = days_of(date_from, date_to)
        N = len(dates)
        prev = days_of(date_from - timedelta(days=N), date_from - timedelta(days=1))
        vals = self.values([h], dates)
        series, missing = [], []
        for d in dates:
            row, _ = self.day_series([h], d)
            reported = (h, d) in self.reports
            exp = self.expected(h, d)
            row["reported"] = reported
            row["expected"] = exp
            row["pending"] = exp and not reported and d == TODAY
            if exp and not reported and d != TODAY:
                missing.append(d.isoformat())
            series.append(row)
        types = []
        for t in TYPES:
            row = self.type_row([h], dates, t)
            if row["birds_end"] > 0 or row["deaths"] > 0:
                types.append({k: v for k, v in row.items() if not k.startswith("_")})
        return {
            "kpis": self.kpis([h], dates, prev),
            "discipline": {
                "expected": vals["_expected"],
                "reported": vals["_reported_expected"],
                "missing": vals["_expected"] - vals["_reported_expected"],
            },
            "missing_days": missing,
            "series": series,
            "by_poultry_type": types,
        }

    def overview(self, date_from, date_to, threshold=Fraction(5)):
        dates = days_of(date_from, date_to)
        N = len(dates)
        prev = days_of(date_from - timedelta(days=N), date_from - timedelta(days=1))
        H = sorted(self.households)
        series = []
        for d in dates:
            row, count = self.day_series(H, d)
            row["reports"] = count
            row["reported_expected"] = sum(1 for h in H if (h, d) in self.reports and self.expected(h, d))
            row["expected_reports"] = sum(1 for h in H if self.expected(h, d))
            series.append(row)
        center = sum(b.initial_quantity for b in self.batches if b.arrived_at <= date_to)
        for hid, _t, kind, qty, mdate, _b in self.moves:
            if mdate > date_to:
                continue
            if kind == MovementType.DISTRIBUTE:
                center -= qty
            elif kind == MovementType.RETURN:
                center += qty
            elif hid is None:
                center -= qty
        center_deaths = sum(q for hid, _t, k, q, d, _b in self.moves if hid is None and k == "DEATH" and d in dates)
        center_out = sum(q for hid, _t, k, q, d, _b in self.moves if hid is None and k == "OUT" and d in dates)
        by_type = []
        for t in TYPES:
            row = self.type_row(H, dates, t)
            by_type.append({
                "poultry_type": t, "households": row["_households"], "birds_end": row["birds_end"],
                "birds_avg": row["birds_avg"], "deaths": row["deaths"], "distributed": row["_distributed"],
                "mortality_pct": row["mortality_pct"],
            })
        groups = defaultdict(list)
        for h in H:
            groups[(self.households[h].region, self.households[h].district)].append(h)
        by_district = []
        for (region, district), members in groups.items():
            v = self.values(members, dates)
            by_district.append({
                "region": region, "district": district, "households": len(members),
                "households_active": sum(1 for h in members if self.households[h].is_active),
                "birds_end": v["birds_end"], "meat_kg": rhu(v["meat_kg"], 2), "feed_kg": rhu(v["feed_kg"], 2),
                "fcr": rhu(v["fcr"], 2), "deaths": v["deaths"], "reports": v["reports"],
                "mortality_pct": rhu(v["mortality_pct"], 1),
                "report_discipline_pct": rhu(v["report_discipline_pct"], 1),
            })
        by_district.sort(key=lambda r: (-r["birds_end"], r["region"], r["district"]))
        # ranking by FCR (lower is better): ≥ 3 reports and meat > 0
        cands = []
        for h in H:
            v = self.values([h], dates)
            if v["reports"] >= 3 and v["meat_kg"] > 0:
                cands.append((h, v))
        top = sorted(cands, key=lambda c: (c[1]["fcr"], -c[1]["meat_kg"], c[0]))[:5]
        top_ids = {c[0] for c in top}
        bottom = [c for c in sorted(cands, key=lambda c: (-c[1]["fcr"], c[0])) if c[0] not in top_ids][:5]

        def item(c):
            h, v = c
            return {"id": h, "fcr": rhu(v["fcr"], 2), "meat_kg": rhu(v["meat_kg"], 2), "feed_kg": rhu(v["feed_kg"], 2),
                    "reports": v["reports"], "avg_weight_g": rhu(v["avg_weight_g"], 1),
                    "is_active": self.households[h].is_active}

        high = []
        for h in H:
            v = self.values([h], dates)
            if v["birds_avg"] > 0 and v["deaths"] >= 1 and v["mortality_pct"] > threshold:
                high.append((v["mortality_pct"], h, v))
        high.sort(key=lambda x: (-x[0], x[1]))
        return {
            "households": {
                "total": len(H),
                "active": sum(1 for h in H if self.households[h].is_active),
                "with_birds": sum(1 for h in H if self.B(h, date_to) > 0),
                "reporting": sum(1 for h in H if any((h, d) in self.reports for d in dates)),
            },
            "kpis": self.kpis(H, dates, prev),
            "center": {"birds_end": center, "deaths": center_deaths, "out": center_out},
            "series": series,
            "by_poultry_type": by_type,
            "by_district": by_district,
            "top": [item(c) for c in top],
            "bottom": [item(c) for c in bottom],
            "high_mortality": {
                "count": len(high),
                "items": [{"id": h, "birds_avg": rhu(v["birds_avg"], 1), "deaths": v["deaths"],
                           "mortality_pct": rhu(m, 1)} for m, h, v in high[:20]],
            },
        }

    # stock ---------------------------------------------------------------------------------
    @staticmethod
    def tx_rows():
        return [
            (t.pk, t.product_id, t.type, Fraction(t.quantity), Fraction(t.balance_after), t.household_id,
             local_day(t.created_at))
            for t in StockTransaction.objects.order_by("id")
        ]

    @staticmethod
    def active_from(product, rows):
        """min(local created day, first transaction day) of the product."""
        days = [local_day(product.created_at)] + [r[6] for r in rows if r[1] == product.pk]
        return min(days)

    @staticmethod
    def n_eff(date_from, date_to, active_from):
        if active_from is None or active_from <= date_from or active_from > date_to:
            return (date_to - date_from).days + 1
        return (date_to - active_from).days + 1

    def cover(self, balance, out_qty, N, today):
        daily = Fraction(out_qty) / N
        cover = None if not daily else Fraction(balance) / daily
        stockout = None if cover is None or cover > 3650 else (today + timedelta(days=floor(cover))).isoformat()
        return daily, cover, stockout

    def product(self, product, date_from, date_to, today=TODAY):
        rows = [r for r in self.tx_rows() if r[1] == product.pk]
        dates = days_of(date_from, date_to)
        N = len(dates)
        before = [r for r in rows if r[6] < date_from]
        opening = before[-1][4] if before else Fraction(0)
        inside = [r for r in rows if date_from <= r[6] <= date_to]
        in_qty = sum((r[3] for r in inside if r[2] == "IN"), Fraction(0))
        out_qty = sum((r[3] for r in inside if r[2] == "OUT"), Fraction(0))
        # V3 §5.6: inventory adjustments are separate keys (never IN/OUT) but move the balance.
        adj_in = sum((r[3] for r in inside if r[2] == "ADJUST_IN"), Fraction(0))
        adj_out = sum((r[3] for r in inside if r[2] == "ADJUST_OUT"), Fraction(0))
        upto = [r for r in rows if r[6] <= date_to]
        closing_last = upto[-1][4] if upto else Fraction(0)
        series, bal = [], opening
        for d in dates:
            i = sum((r[3] for r in inside if r[6] == d and r[2] == "IN"), Fraction(0))
            o = sum((r[3] for r in inside if r[6] == d and r[2] == "OUT"), Fraction(0))
            a = sum((r[3] if r[2] == "ADJUST_IN" else -r[3] for r in inside if r[6] == d and r[2].startswith("ADJUST")),
                    Fraction(0))
            bal += i - o + a
            series.append({"date": d.isoformat(), "in_qty": rhu(i, 2), "out_qty": rhu(o, 2), "adjust_qty": rhu(a, 2),
                           "balance": rhu(bal, 2)})
        N_eff = self.n_eff(date_from, date_to, self.active_from(product, rows))
        daily, cover, stockout = self.cover(product.balance, out_qty, N_eff, today)
        cons = defaultdict(lambda: [Fraction(0), 0])
        unassigned = Fraction(0)
        for r in inside:
            if r[2] != "OUT":
                continue
            if r[5] is None:
                unassigned += r[3]
            else:
                cons[r[5]][0] += r[3]
                cons[r[5]][1] += 1
        consumers = sorted(cons.items(), key=lambda kv: (-kv[1][0], kv[0]))[:10]
        return {
            "totals": {
                "opening_balance": rhu(opening, 2), "in_qty": rhu(in_qty, 2),
                "in_count": sum(1 for r in inside if r[2] == "IN"), "out_qty": rhu(out_qty, 2),
                "out_count": sum(1 for r in inside if r[2] == "OUT"), "net": rhu(in_qty - out_qty, 2),
                "adjust_in_qty": rhu(adj_in, 2), "adjust_out_qty": rhu(adj_out, 2),
                "closing_balance": rhu(opening + in_qty - out_qty + adj_in - adj_out, 2),
            },
            "_closing_last": rhu(closing_last, 2),
            "avg_daily_out": rhu(daily, 3),
            "days_of_cover": rhu(cover, 1),
            "stockout_date": stockout,
            "series": series,
            "top_consumers": [
                {"household_id": h, "quantity": rhu(q, 2), "share_pct": rhu(pct(q, out_qty), 1), "transactions": n}
                for h, (q, n) in consumers
            ],
            "out_unassigned_qty": rhu(unassigned, 2),
        }

    def warehouse(self, date_from, date_to, today=TODAY):
        dates = days_of(date_from, date_to)
        N = len(dates)
        all_rows = self.tx_rows()
        rows = [r for r in all_rows if date_from <= r[6] <= date_to]
        products = list(Product.objects.order_by("name", "id"))
        feed_ids = {p.pk for p in products if p.category == "FEED" and p.unit == "kg"}
        items = []
        for p in products:
            mine = [r for r in rows if r[1] == p.pk]
            if not p.is_active and not mine:
                continue
            i = sum((r[3] for r in mine if r[2] == "IN"), Fraction(0))
            o = sum((r[3] for r in mine if r[2] == "OUT"), Fraction(0))
            ai = sum((r[3] for r in mine if r[2] == "ADJUST_IN"), Fraction(0))
            ao = sum((r[3] for r in mine if r[2] == "ADJUST_OUT"), Fraction(0))
            active = self.active_from(p, all_rows)
            daily, cover, stockout = self.cover(p.balance, o, self.n_eff(date_from, date_to, active), today)
            items.append({
                "id": p.pk, "name": p.name, "category": p.category, "unit": p.unit,
                "balance": rhu(Fraction(p.balance), 2), "min_stock": rhu(Fraction(p.min_stock), 2),
                "is_low": p.balance <= p.min_stock, "is_active": p.is_active, "in_qty": rhu(i, 2),
                "out_qty": rhu(o, 2), "adjust_in_qty": rhu(ai, 2), "adjust_out_qty": rhu(ao, 2),
                "avg_daily_out": rhu(daily, 3), "days_of_cover": rhu(cover, 1),
                "stockout_date": stockout, "_i": i, "_o": o, "_ai": ai, "_ao": ao, "_b": Fraction(p.balance), "_cover": cover,
                "_active": active,
            })
        groups = defaultdict(list)
        for it in items:
            groups[(it["category"], it["unit"])].append(it)
        by_cat = []
        for key in sorted(groups, key=lambda k: (CATEGORY_ORDER.index(k[0]), UNIT_ORDER.index(k[1]))):
            members = groups[key]
            bal = sum((m["_b"] for m in members), Fraction(0))
            o = sum((m["_o"] for m in members), Fraction(0))
            n_eff = self.n_eff(date_from, date_to, min(m["_active"] for m in members))
            daily, cover, _ = self.cover(bal, o, n_eff, today)
            by_cat.append({"category": key[0], "unit": key[1], "products": len(members), "balance": rhu(bal, 2),
                           "in_qty": rhu(sum((m["_i"] for m in members), Fraction(0)), 2), "out_qty": rhu(o, 2),
                           "adjust_in_qty": rhu(sum((m["_ai"] for m in members), Fraction(0)), 2),
                           "adjust_out_qty": rhu(sum((m["_ao"] for m in members), Fraction(0)), 2),
                           "avg_daily_out": rhu(daily, 3), "days_of_cover": rhu(cover, 1)})
        feed_items = [it for it in items if it["id"] in feed_ids]
        feed_bal = sum((Fraction(p.balance) for p in products if p.pk in feed_ids and p.is_active), Fraction(0))
        feed_in = sum((it["_i"] for it in feed_items), Fraction(0))
        feed_out = sum((it["_o"] for it in feed_items), Fraction(0))
        feed_active = min((it["_active"] for it in feed_items), default=None)
        fd, fc, fs = self.cover(feed_bal, feed_out, self.n_eff(date_from, date_to, feed_active), today)
        series = []
        for d in dates:
            day = [r for r in rows if r[6] == d]
            series.append({
                "date": d.isoformat(),
                "in_count": sum(1 for r in day if r[2] == "IN"),
                "out_count": sum(1 for r in day if r[2] == "OUT"),
                "feed_in_kg": rhu(sum((r[3] for r in day if r[2] == "IN" and r[1] in feed_ids), Fraction(0)), 2),
                "feed_out_kg": rhu(sum((r[3] for r in day if r[2] == "OUT" and r[1] in feed_ids), Fraction(0)), 2),
            })
        low = sorted([it for it in items if it["is_active"] and it["is_low"]],
                     key=lambda it: (it["_cover"] is None, it["_cover"] or 0, it["name"]))
        cons = defaultdict(lambda: [Fraction(0), 0])
        for r in rows:
            if r[2] == "OUT" and r[5] is not None and r[1] in feed_ids:
                cons[r[5]][0] += r[3]
                cons[r[5]][1] += 1
        consumers = sorted(cons.items(), key=lambda kv: (-kv[1][0], kv[0]))[:10]
        return {
            "products": {"total": len(products), "active": sum(p.is_active for p in products),
                         "low": sum(1 for p in products if p.is_active and p.balance <= p.min_stock)},
            "feed": {"balance_kg": rhu(feed_bal, 2), "in_kg": rhu(feed_in, 2), "out_kg": rhu(feed_out, 2),
                     "avg_daily_out_kg": rhu(fd, 3), "days_of_cover": rhu(fc, 1), "stockout_date": fs},
            "series": series,
            "by_category": by_cat,
            "items": [{k: v for k, v in it.items() if not k.startswith("_")} for it in items],
            "low_stock": [it["id"] for it in low],
            "top_consumers": [{"household_id": h, "feed_kg": rhu(q, 2), "share_pct": rhu(pct(q, feed_out), 1),
                               "transactions": n} for h, (q, n) in consumers],
        }


# --- randomised fixture ----------------------------------------------------------------------

DISTRICTS = [("Samarqand viloyati", "Navobod tumani"), ("Samarqand viloyati", "Urgut tumani"),
             ("Buxoro viloyati", "Navobod tumani")]


def build_fixture(seed: int):
    rng = random.Random(seed)
    admin = make_user(f"admin{seed}", Role.ADMIN)
    start = TODAY - timedelta(days=200)
    batches = []
    for i, t in enumerate(TYPES * 2):
        arrived = start + timedelta(days=rng.randint(0, 120))
        with mock.patch("django.utils.timezone.now", return_value=local_dt(arrived)):
            batches.append(farm_services.create_batch(
                actor=admin, poultry_type=t, breed="Zot", initial_quantity=rng.randint(800, 3000),
                age_days=rng.randint(1, 30), supplier="S", arrived_at=arrived))
    households = []
    for i in range(9):
        h = make_household(f"Xonadon {seed}-{i}", f"own{seed}_{i}")
        region, district = DISTRICTS[i % 3]
        created = start + timedelta(days=rng.randint(0, 190))
        # some households created at 23:30 local time (UTC previous/same day boundary)
        created_dt = local_dt(created, 23, 30) if i % 2 else local_dt(created, 0, 15)
        Household.objects.filter(pk=h.pk).update(region=region, district=district, created_at=created_dt)
        h.refresh_from_db()
        households.append(h)

    # chronological movements so that every service validation passes
    events = []
    for _ in range(140):
        events.append(start + timedelta(days=rng.randint(0, 200)))
    events.sort()
    for day in events:
        b = rng.choice(batches)
        if day < b.arrived_at:
            continue
        h = rng.choice(households)
        kind = rng.choices(["DISTRIBUTE", "RETURN", "DEATH", "OUT", "CDEATH", "COUT"], [6, 1, 3, 1, 1, 1])[0]
        try:
            if kind in ("CDEATH", "COUT"):
                farm_services.record_movement(actor=admin, batch=b, household=None,
                                              movement_type=kind[1:], quantity=rng.randint(1, 5), on_date=day)
            else:
                qty = rng.randint(50, 300) if kind == "DISTRIBUTE" else rng.randint(1, 40)
                farm_services.record_movement(actor=admin, batch=b, household=h, movement_type=kind,
                                              quantity=qty, on_date=day)
        except Exception:  # not enough birds etc. — skipped like a rejected request
            continue
    # back-dated deaths (dated earlier than "now" but after the distribution: counts stay ≥ 0 per type)
    for h in households:
        for row in farm_services.selectors.household_batches(h.pk):
            first = PoultryMovement.objects.filter(household=h, batch_id=row["batch_id"], type="DISTRIBUTE") \
                .order_by("date").first()
            if first and rng.random() < 0.5:
                b = PoultryBatch.objects.get(pk=row["batch_id"])
                try:
                    farm_services.record_movement(actor=admin, batch=b, household=h, movement_type="DEATH",
                                                  quantity=1, on_date=first.date + timedelta(days=rng.randint(0, 3)))
                except ValidationError:  # a later RETURN / OUT emptied that day: rejected like the API would
                    pass
    # LEGACY row: one back-dated death of a batch BEFORE its distribution to the household. Phase 1 validated
    # only the current count; the service now rejects it, so it is inserted directly (old databases may
    # still contain such rows and every endpoint must keep matching the oracle with a negative past count).
    h = households[3]
    rows = farm_services.selectors.household_batches(h.pk)
    if rows:
        row = rows[-1]
        first = PoultryMovement.objects.filter(household=h, batch_id=row["batch_id"], type="DISTRIBUTE")             .order_by("date").first()
        b = PoultryBatch.objects.get(pk=row["batch_id"])
        back = max(b.arrived_at, first.date - timedelta(days=20))
        if back < first.date:
            PoultryMovement.objects.create(batch=b, household=h, type="DEATH", quantity=min(row["count"], 30),
                                           date=back, created_by=admin)
    # deactivate two households after their history
    for h in households[:2]:
        Household.objects.filter(pk=h.pk).update(is_active=False)
    # reports (some with deaths creating DEATH movements on that day)
    for h in households:
        for offset in range(0, 201):  # includes today
            day = start + timedelta(days=offset)
            if rng.random() < 0.55:
                continue
            feed = Decimal(rng.randint(0, 9000)) / 100
            # Meat on 3 days of 4 with a household-specific conversion (deterministic, no extra rng draws).
            conversion = Decimal(15 + h.pk % 7) / 10
            meat = (feed / conversion).quantize(Decimal("0.01")) if (h.pk + offset) % 4 else None
            draw = rng.randint(0, 600)
            weight = Decimal(1000 + 3 * draw) / 2 if draw % 3 else None  # some days without a weighing
            DailyReport.objects.create(household=h, date=day, bird_count=rng.randint(0, 999), feed_kg=feed,
                                       deaths=0, created_by=admin, meat_kg=meat, avg_weight_g=weight)
    # stock
    products = []
    specs = [("Yem A", "FEED", "kg"), ("Yem B", "FEED", "kg"), ("Yem qop", "FEED", "qop"),
             ("Vaksina", "VACCINE", "dona"), ("Dori", "VET_DRUG", "litr"), ("Eski yem", "FEED", "kg")]
    for name, cat, unit in specs:
        products.append(Product.objects.create(name=f"{name} {seed}", category=cat, unit=unit,
                                               min_stock=Decimal(rng.randint(0, 400))))
    times = []
    for _ in range(160):
        day = TODAY - timedelta(days=rng.randint(0, 120))
        # bias to the local-midnight boundary: 18:59:59Z / 19:00:00Z / 18:30Z (23:30 local)
        choice = rng.random()
        if choice < 0.2:
            moment = datetime.combine(day, time(18, 59, 59), tzinfo=dt_timezone.utc)
        elif choice < 0.4:
            moment = datetime.combine(day, time(19, 0, 0), tzinfo=dt_timezone.utc)
        elif choice < 0.5:
            moment = datetime.combine(day, time(18, 30, 0), tzinfo=dt_timezone.utc)
        else:
            moment = datetime.combine(day, time(rng.randint(0, 23), rng.randint(0, 59)), tzinfo=dt_timezone.utc)
        if moment < timezone.now():
            times.append(moment)
    times.sort()
    for moment in times:
        p = rng.choice(products)
        p.refresh_from_db()
        if not p.is_active:
            continue
        whole = p.unit in ("dona", "qop")
        qty = Decimal(rng.randint(1, 300)) if whole else Decimal(rng.randint(1, 30000)) / 100
        kind = "IN" if (p.balance < qty or rng.random() < 0.35) else "OUT"
        household = rng.choice(households + [None]) if kind == "OUT" else None
        if household is not None and not household.is_active:
            household = None
        with mock.patch("django.utils.timezone.now", return_value=moment):
            warehouse_services.record_stock_transaction(
                actor=admin, product_id=p.pk, transaction_type=kind, quantity=qty, document_no="D", household=household)
    Product.objects.filter(pk=products[-1].pk).update(is_active=False)
    return admin, households, batches, products


class OracleMixin:
    SEED = 1

    @classmethod
    def setUpTestData(cls):
        cls.admin, cls.households, cls.batches, cls.products = build_fixture(cls.SEED)

    def get(self, url):
        self.login_as(self.admin)
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200, response.content[:500])
        return response.json()

    PERIODS = [
        (TODAY - timedelta(days=29), TODAY),
        (TODAY - timedelta(days=6), TODAY),
        (TODAY - timedelta(days=90), TODAY - timedelta(days=40)),
        (TODAY - timedelta(days=150), TODAY - timedelta(days=150)),
        (TODAY - timedelta(days=365), TODAY),
    ]

    def test_overview_matches_oracle(self):
        oracle = Oracle()
        for date_from, date_to in self.PERIODS:
            for threshold in ("5", "0.5"):
                with self.subTest(period=(date_from, date_to), threshold=threshold):
                    data = self.get(f"/api/stats/overview?date_from={date_from}&date_to={date_to}"
                                    f"&mortality_threshold={threshold}")
                    expected = oracle.overview(date_from, date_to, Fraction(threshold))
                    self.assertEqual(data["households"], expected["households"])
                    self.assertEqual(data["kpis"], expected["kpis"])
                    self.assertEqual(data["center"], expected["center"])
                    self.assertEqual(data["series"], expected["series"])
                    got_types = [{k: v for k, v in row.items() if k != "poultry_type_display"}
                                 for row in data["by_poultry_type"]]
                    self.assertEqual(got_types, expected["by_poultry_type"])
                    self.assertEqual(data["by_district"], expected["by_district"])
                    for key, exp_key in (("top_households", "top"), ("bottom_households", "bottom")):
                        got = [{"id": r["household"]["id"], "fcr": r["fcr"], "meat_kg": r["meat_kg"],
                                "feed_kg": r["feed_kg"], "reports": r["reports"], "avg_weight_g": r["avg_weight_g"],
                                "is_active": r["is_active"]} for r in data[key]]
                        self.assertEqual(got, expected[exp_key])
                    hm = data["high_mortality"]
                    self.assertEqual(hm["count"], expected["high_mortality"]["count"])
                    self.assertEqual(
                        [{"id": r["household"]["id"], "birds_avg": r["birds_avg"], "deaths": r["deaths"],
                          "mortality_pct": r["mortality_pct"]} for r in hm["items"]],
                        expected["high_mortality"]["items"])

    def test_household_stats_match_oracle(self):
        oracle = Oracle()
        for h in self.households:
            for date_from, date_to in self.PERIODS:
                with self.subTest(household=h.pk, period=(date_from, date_to)):
                    data = self.get(f"/api/households/{h.pk}/stats?date_from={date_from}&date_to={date_to}")
                    expected = oracle.household(h.pk, date_from, date_to)
                    self.assertEqual(data["kpis"], expected["kpis"])
                    self.assertEqual(data["discipline"], expected["discipline"])
                    self.assertEqual(data["missing_days"], expected["missing_days"])
                    self.assertEqual(data["series"], expected["series"])
                    got_types = [{k: v for k, v in row.items() if k != "poultry_type_display"}
                                 for row in data["by_poultry_type"]]
                    self.assertEqual(got_types, expected["by_poultry_type"])

    def test_product_stats_match_oracle(self):
        oracle = Oracle()
        for p in Product.objects.all():
            for date_from, date_to in self.PERIODS:
                with self.subTest(product=p.name, period=(date_from, date_to)):
                    data = self.get(f"/api/products/{p.pk}/stats?date_from={date_from}&date_to={date_to}")
                    expected = oracle.product(p, date_from, date_to)
                    self.assertEqual(data["totals"], expected["totals"])
                    self.assertEqual(data["totals"]["closing_balance"], expected["_closing_last"])
                    for key in ("avg_daily_out", "days_of_cover", "stockout_date", "series", "out_unassigned_qty"):
                        self.assertEqual(data[key], expected[key], key)
                    self.assertEqual(
                        [{"household_id": r["household"]["id"], "quantity": r["quantity"], "share_pct": r["share_pct"],
                          "transactions": r["transactions"]} for r in data["top_consumers"]],
                        expected["top_consumers"])

    def test_warehouse_stats_match_oracle(self):
        oracle = Oracle()
        for date_from, date_to in self.PERIODS:
            with self.subTest(period=(date_from, date_to)):
                data = self.get(f"/api/warehouse/stats?date_from={date_from}&date_to={date_to}")
                expected = oracle.warehouse(date_from, date_to)
                self.assertEqual(data["products"], expected["products"])
                self.assertEqual(data["feed"], expected["feed"])
                self.assertEqual(data["series"], expected["series"])
                self.assertEqual([{k: v for k, v in r.items() if k != "category_display"} for r in data["by_category"]],
                                 expected["by_category"])
                self.assertEqual([{k: v for k, v in r.items() if k != "category_display"} for r in data["items"]],
                                 expected["items"])
                self.assertEqual([r["id"] for r in data["low_stock"]], expected["low_stock"])
                self.assertEqual(
                    [{"household_id": r["household"]["id"], "feed_kg": r["feed_kg"], "share_pct": r["share_pct"],
                      "transactions": r["transactions"]} for r in data["top_consumers"]],
                    expected["top_consumers"])


class OracleSeed1Tests(OracleMixin, FrozenClockTestCase):
    SEED = 1


class OracleSeed2Tests(OracleMixin, FrozenClockTestCase):
    SEED = 2


class OracleSeed3Tests(OracleMixin, FrozenClockTestCase):
    SEED = 3


class OracleFixtureSanityTests(FrozenClockTestCase):
    """The randomised fixture really exercises the interesting cases (guards against a vacuous oracle)."""

    @classmethod
    def setUpTestData(cls):
        cls.admin, cls.households, cls.batches, cls.products = build_fixture(1)

    def test_fixture_is_rich(self):
        self.assertGreater(PoultryMovement.objects.count(), 60)
        self.assertTrue(PoultryMovement.objects.filter(household__isnull=True).exists())
        self.assertTrue(PoultryMovement.objects.filter(type="RETURN").exists())
        self.assertGreater(DailyReport.objects.count(), 500)
        self.assertGreater(StockTransaction.objects.count(), 100)
        boundary = [t for t in StockTransaction.objects.all()
                    if t.created_at.astimezone(dt_timezone.utc).time() in (time(18, 59, 59), time(19, 0))]
        self.assertGreater(len(boundary), 10)
        mixed = [h for h in self.households
                 if PoultryMovement.objects.filter(household=h).values("batch__poultry_type").distinct().count() > 1]
        self.assertTrue(mixed)
        self.login_as(self.admin)
        data = self.client.get(f"/api/stats/overview?date_from={TODAY - timedelta(days=150)}&date_to={TODAY}"
                               "&mortality_threshold=0.5").json()
        self.assertTrue(data["top_households"])
        self.assertTrue(data["bottom_households"])
        self.assertTrue(data["high_mortality"]["items"])
        self.assertGreater(data["kpis"]["birds_end"]["value"], 0)
        self.assertIsNotNone(data["kpis"]["fcr"]["value"])
        self.assertIsNotNone(data["kpis"]["avg_weight_g"]["value"])
        self.assertGreater(data["kpis"]["meat_kg"]["value"], 0)

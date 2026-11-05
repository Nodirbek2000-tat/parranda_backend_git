"""``problem_households`` and ``expiring_lots`` of ``GET /api/dashboard/admin`` (API_CONTRACT_V3 §10.1).

Problem household rules (``today`` local, thresholds from ``SystemSettings``):

- candidates: active households with ``B_h(today) > 0``; OPEN_DISEASE and OVERDUE_VACCINATION also apply
  without birds;
- ``NO_REPORT``: ``days = today − last report date`` (report dated ``≤ today``); without any report
  ``days = today − first DISTRIBUTE date``; flagged when ``days ≥ problem_report_days``;
- ``HIGH_MORTALITY``: ``d7`` = household DEATH movements dated ``today − 6 … today``,
  ``base = B_h(today) + d7``, ``pct = 100 × d7 / base``; flagged when ``base > 0`` and the unrounded
  ``pct > mortality_alert_threshold_pct``;
- ``OVERDUE_VACCINATION``: PLANNED vaccinations with ``planned_date < today``;
- ``OPEN_DISEASE``: disease records with status ≠ RESOLVED.

Order: number of reasons desc, NO_REPORT days desc, id. ``items`` ≤ 10, ``count`` = all.
Three queries (settings, one annotated household query, lots), independent of the data size.
"""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal
from fractions import Fraction
from typing import Any

from django.db.models import Count, DateField, IntegerField, Max, Min, OuterRef, Subquery, Sum
from django.db.models.functions import Coalesce
from django.utils import timezone

from farm.models import DailyReport, Household, MovementType, PoultryMovement
from stats import metrics, queries
from warehouse import lots as lot_reads
from warehouse.readiness import models_ready, optional_model

PROBLEM_LIMIT = 10
EXPIRING_LIMIT = 5


def _reason(code: str, label: str, value: int | float) -> dict[str, Any]:
    return {"code": code, "label": label, "value": value}


def _aggregate(qs, field_name: str, function, output_field):
    """Correlated subquery ``function(field)`` per household (``0`` / ``NULL`` when there is no row)."""
    return Subquery(
        qs.order_by().values("household_id").annotate(v=function(field_name)).values("v")[:1],
        output_field=output_field,
    )


def problem_households(today: date, settings) -> dict[str, Any]:
    """ONE query: active households annotated with every input of the rules (correlated subqueries)."""
    week_start = today - timedelta(days=6)
    movements = PoultryMovement.objects.filter(household_id=OuterRef("pk"), date__lte=today)
    annotations = {
        "birds": Coalesce(
            Subquery(
                movements.order_by().values("household_id").annotate(v=Sum(queries.HOUSEHOLD_DELTA)).values("v")[:1],
                output_field=IntegerField(),
            ),
            0,
        ),
        "d7": Coalesce(
            _aggregate(movements.filter(type=MovementType.DEATH, date__gte=week_start), "quantity", Sum, IntegerField()),
            0,
        ),
        "first_distribute": _aggregate(movements.filter(type=MovementType.DISTRIBUTE), "date", Min, DateField()),
        "last_report": _aggregate(
            DailyReport.objects.filter(household_id=OuterRef("pk"), date__lte=today), "date", Max, DateField()
        ),
    }
    vaccination = optional_model("vet", "Vaccination", "household", "status", "planned_date")
    if vaccination is not None:
        overdue = vaccination.objects.filter(household_id=OuterRef("pk"), status="PLANNED", planned_date__lt=today)
        annotations["overdue"] = Coalesce(_aggregate(overdue, "id", Count, IntegerField()), 0)
    disease = optional_model("vet", "DiseaseRecord", "household", "status")
    if disease is not None:
        open_diseases = disease.objects.filter(household_id=OuterRef("pk")).exclude(status="RESOLVED")
        annotations["diseases"] = Coalesce(_aggregate(open_diseases, "id", Count, IntegerField()), 0)
    rows = list(
        Household.objects.filter(is_active=True).order_by("id").annotate(**annotations).values(
            "id", "name", "district", *annotations
        )
    )
    report_days = int(settings.problem_report_days)
    threshold = Fraction(Decimal(settings.mortality_alert_threshold_pct))
    items = []
    for row in rows:
        household_id, name, district = row["id"], row["name"], row["district"]
        birds, d7, first_distribute = row["birds"], row["d7"], row["first_distribute"]
        reasons = []
        no_report_days = -1
        if birds > 0:
            last = row["last_report"]
            start = last or first_distribute
            if start is not None:
                no_report_days = (today - start).days
                if no_report_days >= report_days:
                    reasons.append(_reason("NO_REPORT", f"{no_report_days} kundan beri hisobot yo‘q", no_report_days))
            base = birds + d7
            if base > 0 and d7 > 0 and Fraction(100 * d7, base) > threshold:
                pct = metrics.round_half_up(metrics.percent(d7, base), 1)
                reasons.append(_reason("HIGH_MORTALITY", f"7 kunlik o‘lim {pct:.1f}%", pct))
        if row.get("overdue"):
            count = row["overdue"]
            reasons.append(_reason("OVERDUE_VACCINATION", f"Muddati o‘tgan emlash: {count}", count))
        if row.get("diseases"):
            count = row["diseases"]
            reasons.append(_reason("OPEN_DISEASE", f"Ochiq kasallik: {count}", count))
        if reasons:
            items.append(
                (
                    -len(reasons),
                    -no_report_days if any(r["code"] == "NO_REPORT" for r in reasons) else 1,
                    household_id,
                    {
                        "household": {"id": household_id, "code": f"XN-{household_id:04d}", "name": name},
                        "district": district,
                        "reasons": reasons,
                    },
                )
            )
    items.sort(key=lambda item: item[:3])
    return {"count": len(items), "items": [item[3] for item in items[:PROBLEM_LIMIT]]}


def expiring_lots(today: date, settings) -> dict[str, Any]:
    from warehouse.models import StockLot, StockLotAllocation
    from warehouse.serializers import StockLotSerializer

    if not models_ready(StockLot, StockLotAllocation):
        return {"count": 0, "items": []}
    soon = int(settings.expiry_reminder_days)
    qs = lot_reads.expiring_lots(today, soon)
    items = list(qs[:EXPIRING_LIMIT])
    count = len(items) if len(items) < EXPIRING_LIMIT else qs.count()
    data = StockLotSerializer(items, many=True, context={"today": today, "soon_days": soon}).data
    return {"count": count, "items": data}


def admin_dashboard_block(today: date | None = None) -> dict[str, Any]:
    from core.models import SystemSettings
    from core.settings_service import get_settings

    today = today or timezone.localdate()
    # Defaults (unsaved row) while the core tables are not migrated yet (live server before the Fix phase).
    settings = get_settings() if models_ready(SystemSettings) else SystemSettings()
    return {
        "problem_households": problem_households(today, settings),
        "expiring_lots": expiring_lots(today, settings),
    }

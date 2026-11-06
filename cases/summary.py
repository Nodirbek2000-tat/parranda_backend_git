"""Per-case key numbers for the "Xonadonlar" list: birds, days since delivery, feed received, vet visits,
cost so far and (estimated) revenue. Computed for a whole page of cases with a few grouped queries.
"""

from collections import defaultdict
from decimal import Decimal

from django.conf import settings
from django.db.models import Count
from django.utils import timezone

from stock.models import Category, StockMove

from . import stages as chain
from .finance import finance
from .models import CaseStatus, StageStatus, VetVisit, VisitStatus


def _num(value) -> float | int:
    value = Decimal(value)
    return int(value) if value == value.to_integral_value() else float(round(value, 2))


def summaries(cases) -> dict[int, dict]:
    cases = list(cases)
    ids = [case.pk for case in cases]
    if not ids:
        return {}

    # feed received per case and unit (OUT minus returns), package feed included
    feed: dict[int, dict[str, Decimal]] = defaultdict(lambda: defaultdict(Decimal))
    rows = StockMove.objects.filter(case_id__in=ids, product__category=Category.YEM).values_list(
        "case_id", "product__unit", "type", "qty")
    for case_id, unit, kind, qty in rows:
        feed[case_id][unit] += qty if kind == StockMove.OUT else -qty

    # latest done visit (bird count) and number of done visits
    visits_done = dict(VetVisit.objects.filter(case_id__in=ids, status=VisitStatus.DONE)
                       .values_list("case_id").annotate(n=Count("id")).values_list("case_id", "n"))
    latest_count: dict[int, int] = {}
    for case_id, count in (VetVisit.objects.filter(case_id__in=ids, status=VisitStatus.DONE,
                                                   chicks_count__isnull=False)
                           .order_by("case_id", "-done_at").values_list("case_id", "chicks_count")):
        latest_count.setdefault(case_id, count)

    kg_per_bird = Decimal(str(settings.EST_KG_PER_BIRD))
    price_per_kg = int(settings.EST_PRICE_PER_KG)
    today = timezone.localdate()
    result = {}
    for case in cases:
        stages = {st.number: st for st in case.stages.all()}
        data = {n: (st.data or {}) for n, st in stages.items()}

        delivery = stages.get(chain.DELIVERY_STAGE)
        delivered_at = None
        if delivery and delivery.status in (StageStatus.SUBMITTED, StageStatus.DONE):
            delivered_at = delivery.submitted_at or delivery.decided_at
        received = next((v for v in (data.get(chain.OWNER_STAGE, {}).get("received_count"),
                                     data.get(chain.DELIVERY_STAGE, {}).get("delivered_healthy"))
                         if isinstance(v, int)), None)
        birds = latest_count.get(case.pk, received)
        days = (today - timezone.localdate(delivered_at)).days if delivered_at else None
        # once the birds left for the slaughterhouse the counter stops
        pickup = stages.get(chain.GROWING_STAGE + 1)
        if days is not None and pickup and pickup.submitted_at:
            days = (timezone.localdate(pickup.submitted_at) - timezone.localdate(delivered_at)).days

        fin = finance(case)
        done = case.status == CaseStatus.DONE and fin.get("revenue") is not None
        estimate = int(Decimal(birds) * kg_per_bird * price_per_kg) if birds else None
        result[case.pk] = {
            "birds_received": received,
            "birds": birds,
            "delivered_at": delivered_at,
            "days_since_delivery": days,
            "growing_days": chain.growing_days(),
            "feed": [{"unit": unit, "qty": _num(qty)} for unit, qty in sorted(feed[case.pk].items()) if qty],
            "vet_visits": visits_done.get(case.pk, 0),
            "total_cost": fin["total_cost"],
            "revenue": fin["revenue"] if done else estimate,
            "revenue_is_estimate": not done,
            "profit": (fin["revenue"] if done else (estimate or 0)) - fin["total_cost"] if (done or estimate) else None,
        }
    return result

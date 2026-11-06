"""Finance of one case (v2.1 §6), computed from primary data. All amounts are so‘m integers.

- package_cost: the package price, once the package was issued (stage 5 SUBMITTED or DONE).
- chicks_cost: stage 6 chicks_count × settings.CHICK_PRICE (DEMO).
- feed_cost / vet_cost / other_cost: net stock OUT linked to the case, by product category (yem / dori / rest),
  EXCLUDING the stage-5 package moves: the package price already covers those items, so they are shown only as
  the informational `package_items_value` and never counted twice.
- revenue: stage 13 (null before it is submitted); profit = revenue - total_cost.
"""

from decimal import ROUND_HALF_UP, Decimal

from django.conf import settings

from stock.models import Category, StockMove

from . import stages as chain
from .models import StageStatus

SUBMITTED_OR_DONE = (StageStatus.SUBMITTED, StageStatus.DONE)
COST_KEYS = ("package_cost", "chicks_cost", "feed_cost", "vet_cost", "other_cost")


def _int(value) -> int:
    return int(Decimal(value).quantize(Decimal(1), rounding=ROUND_HALF_UP))


def _stages(case) -> dict:
    cached = getattr(case, "_prefetched_objects_cache", {}).get("stages")
    rows = cached if cached is not None else case.stages.all()
    return {st.number: st for st in rows}


def _moves_value(case) -> tuple[dict, Decimal]:
    """(net value per bucket for non-package moves, net value of package moves)."""
    buckets = {"feed_cost": Decimal(0), "vet_cost": Decimal(0), "other_cost": Decimal(0)}
    package = Decimal(0)
    rows = StockMove.objects.filter(case=case).values_list("purpose", "type", "qty", "product__price",
                                                           "product__category")
    for purpose, kind, qty, price, category in rows:
        value = qty * price * (1 if kind == StockMove.OUT else -1)
        if purpose == StockMove.PACKAGE:
            package += value
        elif category == Category.YEM:
            buckets["feed_cost"] += value
        elif category == Category.DORI:
            buckets["vet_cost"] += value
        else:
            buckets["other_cost"] += value
    return buckets, package


def finance(case, *, revenue=None, price_per_kg=None, final: bool = False) -> dict:
    """Live numbers. `revenue` / `price_per_kg` default to stage 13 data when stage 13 is SUBMITTED or DONE."""
    stages = _stages(case)
    data = {n: (st.data or {}) for n, st in stages.items()}
    warehouse = stages.get(chain.WAREHOUSE_STAGE)
    # a later partiya carries the warehouse stage over: the package (jo‘jaxona) is not paid again
    issued = warehouse and warehouse.status in SUBMITTED_OR_DONE and not (warehouse.data or {}).get("carried_over")
    package_cost = _int(case.package.price) if issued else 0

    chicks = data.get(chain.INCUBATION_STAGE, {}).get("chicks_count")
    chicks = chicks if isinstance(chicks, int) else None
    chick_price = int(settings.CHICK_PRICE)
    buckets, package_value = _moves_value(case)

    accounting = stages.get(chain.ACCOUNTING_STAGE)
    if revenue is None and accounting and accounting.status in SUBMITTED_OR_DONE:
        revenue = data[chain.ACCOUNTING_STAGE].get("revenue")
        price_per_kg = data[chain.ACCOUNTING_STAGE].get("price_per_kg")

    result = {
        "package_cost": package_cost,
        "chicks_count": chicks,
        "chick_price": chick_price,
        "chicks_cost": (chicks or 0) * chick_price,
        **{key: _int(value) for key, value in buckets.items()},
        "package_items_value": _int(package_value),
    }
    total = sum(result[key] for key in COST_KEYS)
    kg = data.get(chain.SLAUGHTER_STAGE, {}).get("total_kg")
    birds = next((v for v in (data.get(chain.SLAUGHTER_STAGE, {}).get("received_count"),
                              data.get(chain.OWNER_STAGE, {}).get("received_count"), chicks)
                  if isinstance(v, int) and v > 0), None)
    revenue = int(revenue) if revenue is not None else None
    if price_per_kg is None and revenue is not None and kg:
        price_per_kg = _int(Decimal(revenue) / Decimal(str(kg)))
    result.update({
        "total_cost": total,
        "revenue": revenue,
        "profit": revenue - total if revenue is not None else None,
        "birds": birds,
        "per_bird_cost": _int(Decimal(total) / birds) if birds else None,
        "kg": float(kg) if kg else None,
        "price_per_kg": int(price_per_kg) if price_per_kg is not None else None,
        "is_final": final,
    })
    return result


def case_finance(case) -> dict:
    """The snapshot stored at stage 13 submit (when stage 13 is SUBMITTED/DONE), else the live numbers."""
    accounting = _stages(case).get(chain.ACCOUNTING_STAGE)
    if accounting and accounting.status in SUBMITTED_OR_DONE and isinstance((accounting.data or {}).get("finance"),
                                                                            dict):
        return accounting.data["finance"]
    return finance(case)


def has_cost(fin: dict) -> bool:
    return bool(fin["total_cost"]) or fin["revenue"] is not None

"""Stock balance changes. The balance changes only through StockMove rows, inside one transaction,
with the product rows locked by `select_for_update(of=("self",))`."""

from decimal import Decimal

from django.db import transaction
from django.db.models import Case as DbCase, DecimalField, F, Sum, When

from audit.models import log

from .models import Product, StockMove


class StockShortage(Exception):
    """Not enough stock. `missing` = [{product_id, name, unit, need, have}]."""

    def __init__(self, missing: list[dict]):
        super().__init__("Omborda yetarli mahsulot yo‘q")
        self.missing = missing


def _locked(product_ids) -> dict[int, Product]:
    rows = Product.objects.select_for_update(of=("self",)).filter(pk__in=product_ids).order_by("pk")
    return {product.pk: product for product in rows}


def _move(product: Product, kind: str, qty: Decimal, *, user, note: str = "", case=None) -> StockMove:
    product.balance = product.balance + qty if kind == StockMove.IN else product.balance - qty
    product.save(update_fields=["balance"])
    return StockMove.objects.create(
        product=product, type=kind, qty=qty, balance_after=product.balance, case=case, note=note, created_by=user
    )


@transaction.atomic
def stock_in(product_id: int, qty: Decimal, *, user, note: str = "") -> StockMove:
    product = _locked([product_id])[product_id]
    move = _move(product, StockMove.IN, Decimal(qty), user=user, note=note)
    log(user, "stock.in", move, after={"product": product.pk, "qty": str(qty), "balance": str(product.balance)})
    return move


def issued_for_case(case) -> dict[int, Decimal]:
    """Net quantity per product currently issued (OUT minus IN) for `case`."""
    rows = (
        StockMove.objects.filter(case=case)
        .order_by()
        .values("product_id")
        .annotate(net=Sum(DbCase(When(type=StockMove.OUT, then=F("qty")), default=-F("qty"),
                                output_field=DecimalField(max_digits=14, decimal_places=3))))
    )
    return {row["product_id"]: row["net"] for row in rows if row["net"]}


def issue_package(case, *, user) -> list[StockMove]:
    """Stock OUT of the case package items (what is not issued yet). Raises StockShortage, writes nothing then.

    Must run inside the caller's transaction.
    """
    issued = issued_for_case(case)
    needed = {}
    for item in case.package.items.all():
        rest = item.qty - issued.get(item.product_id, Decimal(0))
        if rest > 0:
            needed[item.product_id] = rest
    products = _locked(needed.keys())
    missing = [
        {"product_id": pid, "name": products[pid].name, "unit": products[pid].unit,
         "need": float(qty), "have": float(products[pid].balance)}
        for pid, qty in needed.items()
        if products[pid].balance < qty
    ]
    if missing:
        raise StockShortage(missing)
    note = f"{case.code}: paket berildi"
    return [_move(products[pid], StockMove.OUT, qty, user=user, note=note, case=case) for pid, qty in needed.items()]


def return_package(case, *, user) -> list[StockMove]:
    """Stock IN of everything issued for `case` (the warehouse stage was returned)."""
    issued = {pid: qty for pid, qty in issued_for_case(case).items() if qty > 0}
    products = _locked(issued.keys())
    note = f"{case.code}: bosqich qaytarildi, mahsulot omborga qaytdi"
    return [_move(products[pid], StockMove.IN, qty, user=user, note=note, case=case) for pid, qty in issued.items()]

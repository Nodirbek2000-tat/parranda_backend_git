"""Stock side of logistics tasks (API_CONTRACT_V3 §7.4): reservation, OUT at ``start``, return IN on cancel.

Balances and lots are written only by :func:`warehouse.services.post_locked` (the single posting primitive of the
warehouse module); this module owns the ``Product.reserved`` changes of tasks. Every function runs inside the
task transaction, after the task row lock (lock order §17.1: Order → LogisticsTask → Products ascending →
StockLots ascending — ``post_locked`` locks the lots itself).
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable
from datetime import date
from decimal import Decimal

from django.db.models import F

from rest_framework.exceptions import ValidationError

from common.formatting import fmt_number
from warehouse import services as warehouse
from warehouse.models import Product, StockLotAllocation, StockTransaction, StockTransactionType

ZERO = Decimal("0")
REFERENCE_TYPE = "logistics_task"
MSG_SHORT = "“{name}”: omborda yetarli emas. Bo‘sh qoldiq: {available} {unit}"


def available(product: Product) -> Decimal:
    return product.balance - product.reserved


def short_message(product: Product) -> str:
    return MSG_SHORT.format(name=product.name, available=fmt_number(max(available(product), ZERO)), unit=product.unit)


def lock_products(product_ids: Iterable[int]) -> dict[int, Product]:
    """``SELECT … FOR UPDATE`` in ascending id order."""
    return warehouse.lock_products(product_ids)


def _totals(lines: Iterable[tuple[int, Decimal]]) -> dict[int, Decimal]:
    totals: dict[int, Decimal] = defaultdict(lambda: ZERO)
    for product_id, quantity in lines:
        if quantity > 0:
            totals[int(product_id)] += quantity
    return dict(totals)


def reserve(lines: Iterable[tuple[int, Decimal]]) -> dict[int, Product]:
    """All-or-nothing: each product needs ``available ≥ quantity``; then ``reserved += quantity``."""
    totals = _totals(lines)
    products = lock_products(totals)
    errors = [short_message(products[pk]) for pk, quantity in totals.items() if quantity > available(products[pk])]
    if errors:
        raise ValidationError({"items": errors})
    for pk, quantity in totals.items():
        product = products[pk]
        product.reserved += quantity
        product.save(update_fields=["reserved"])
    return products


def release(lines: Iterable[tuple[int, Decimal]], products: dict[int, Product] | None = None) -> None:
    """``reserved −= quantity`` (never below 0). ``products`` = already locked rows, if the caller has them."""
    totals = _totals(lines)
    if not totals:
        return
    if products is None:
        products = lock_products(totals)
    for pk, quantity in totals.items():
        product = products[pk]
        product.reserved = max(product.reserved - quantity, ZERO)
        product.save(update_fields=["reserved"])


def post_out(*, actor, product: Product, quantity: Decimal, release_qty: Decimal, task) -> StockTransaction:
    """Release the item's reservation, then the OUT (``stock_out`` to WAREHOUSE only, lots FEFO)."""
    if release_qty > 0:
        product.reserved = max(product.reserved - release_qty, ZERO)
        product.save(update_fields=["reserved"])
    if quantity > available(product):
        raise ValidationError({"items": [short_message(product)]})
    return warehouse.post_locked(
        actor=actor,
        product=product,
        transaction_type=StockTransactionType.OUT,
        quantity=quantity,
        document_no=task.code,
        household=task.household,
        note=f"{task.code} yetkazib berish",
        notify_owner=False,
        reference=(REFERENCE_TYPE, task.pk),
    )


def returned_lot(out_transaction_id: int | None) -> tuple[str, date | None]:
    """``(lot_no, expiry_date)`` for goods coming back from an OUT, from its FEFO allocations.

    One IN creates exactly one lot, so a return of goods taken from several lots gets the **earliest** expiry of
    them (conservative: FEFO spends it first and the expiry reminders fire) and the lot numbers joined.
    Untracked stock (no allocation) → ``("", None)`` as before.
    """
    if out_transaction_id is None:
        return "", None
    allocations = list(
        StockLotAllocation.objects.filter(transaction_id=out_transaction_id)
        .select_related("lot")
        .order_by(F("lot__expiry_date").asc(nulls_last=True), "lot_id")
    )
    if not allocations:
        return "", None
    numbers: list[str] = []
    for allocation in allocations:
        if allocation.lot.lot_no and allocation.lot.lot_no not in numbers:
            numbers.append(allocation.lot.lot_no)
    expiries = [allocation.lot.expiry_date for allocation in allocations if allocation.lot.expiry_date is not None]
    return ", ".join(numbers)[:64], (min(expiries) if expiries else None)


def post_return(*, actor, product: Product, quantity: Decimal, task, out_transaction_id: int | None = None) -> StockTransaction:
    """Goods back in the warehouse after a cancel in transit: ``IN`` whose lot keeps the lot number and expiry
    of the lots the task's OUT consumed (see :func:`returned_lot`)."""
    lot_no, expiry_date = returned_lot(out_transaction_id)
    return warehouse.post_locked(
        actor=actor,
        product=product,
        transaction_type=StockTransactionType.IN,
        quantity=quantity,
        document_no=task.code,
        note=f"{task.code} bekor qilindi — omborga qaytarildi",
        lot_no=lot_no,
        expiry_date=expiry_date,
        reference=(REFERENCE_TYPE, task.pk),
    )

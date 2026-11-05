"""Warehouse V3 notifications (API_CONTRACT_V3 §9.4): ``stock_running_low``, ``inventory_approved``,
``lot_expiring`` / ``lot_expired`` (daily reminder jobs, §16.1).

Phase 1 events (``stock_in``, ``stock_out``, ``low_stock``) stay in :mod:`notifications.events`.
Every function is called inside the business transaction; rows are queued after the commit.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import TYPE_CHECKING

from common.formatting import fmt_date, fmt_number
from notifications.compose import Row, compose, qty
from notifications.services import notify, notify_on_commit

if TYPE_CHECKING:
    from .models import InventoryCount, Product, StockLot, StockTransaction

RUNNING_LOW_FACTOR = Decimal("1.5")


def running_low_crossed(product: Product, before: Decimal, after: Decimal) -> bool:
    """Balance crosses from ``> 1.5 × min`` to ``≤ 1.5 × min`` while still ``> min`` (``min > 0``)."""
    minimum = product.min_stock
    if minimum <= 0:
        return False
    limit = minimum * RUNNING_LOW_FACTOR
    return before > limit >= after > minimum


def stock_running_low(product: Product, row: StockTransaction) -> None:
    rows: list[Row] = [
        ("Mahsulot", product.name),
        ("Toifa", product.get_category_display()),
        ("Qoldiq", qty(row.balance_after, product.unit)),
        ("Minimal zaxira", qty(product.min_stock, product.unit)),
        ("Sana", fmt_date(row.created_at)),
    ]
    text = compose("stock_running_low", rows, "Qoldiq kamaymoqda", row.op_id)
    notify_on_commit(
        "stock_running_low",
        key=row.pk,
        text=text,
        staff_path=f"/warehouse/{product.pk}",
        related_op=row.op_id,
        object_type="Product",
        object_id=product.pk,
    )


def inventory_approved(count: InventoryCount, *, lines: int, surplus: int, shortage: int) -> None:
    rows: list[Row] = [
        ("Inventarizatsiya", count.code),
        ("Mahsulotlar", fmt_number(lines)),
        ("Ortiqcha", fmt_number(surplus)),
        ("Kamomad", fmt_number(shortage)),
        ("Tasdiqladi", str(count.approved_by) if count.approved_by_id else None),
        ("Sana", fmt_date(count.approved_at)),
        ("Izoh", count.note),
    ]
    text = compose("inventory_approved", rows, "Tasdiqlandi", count.code)
    notify_on_commit(
        "inventory_approved",
        key=count.pk,
        text=text,
        staff_path=f"/warehouse/inventory/{count.pk}",
        related_op=count.code,
        object_type="InventoryCount",
        object_id=count.pk,
    )


def _lot_rows(lot: StockLot, today: date) -> list[Row]:
    product = lot.product
    days = (lot.expiry_date - today).days
    if days < 0:
        left = f"{fmt_number(-days)} kun oldin o‘tgan"
    elif days == 0:
        left = "Bugun tugaydi"
    else:
        left = f"{fmt_number(days)} kun qoldi"
    return [
        ("Mahsulot", product.name),
        ("Partiya", lot.lot_no or f"#{lot.pk}"),
        ("Yaroqlilik muddati", fmt_date(lot.expiry_date)),
        ("Muddat", left),
        ("Qoldiq", qty(lot.remaining_qty, product.unit)),
    ]


def lot_expiry(lot: StockLot, today: date, *, expired: bool) -> list:
    """Reminder rows of one lot, queued now (the reminder job's transaction); returns the NEW rows only,
    so a second run the same day reports ``queued = 0`` (§16.1 idempotency)."""
    event = "lot_expired" if expired else "lot_expiring"
    text = compose(event, _lot_rows(lot, today), "Muddati o‘tgan" if expired else "Muddati yaqin",
                   lot.in_transaction.op_id)
    return notify(
        event,
        key=f"{lot.pk}.{'expired' if expired else 'soon'}",
        text=text,
        staff_path=f"/warehouse/{lot.product_id}",
        related_op=lot.in_transaction.op_id,
        object_type="Product",
        object_id=lot.product_id,
    )

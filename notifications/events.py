"""Phase 1/2 business events → texts and recipients (API_CONTRACT §4, V3 §9.4) + ``user_created``.

Each function is called inside the business transaction; the rows are queued by
:func:`notifications.services.notify_on_commit` after the commit. Role recipients come from the
registry (and the Super Admin settings); this module only names the contextual users.
V3 modules keep their own ``<app>/events.py`` with the same pattern.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from common.formatting import fmt_date, fmt_number
from common.roles import role_display
from farm.models import MovementType
from warehouse.models import StockTransactionType

from . import registry
from .compose import Row, compose, household_rows, qty
from .services import notify_on_commit

if TYPE_CHECKING:
    from accounts.models import User
    from farm.models import DailyReport, Household, PoultryBatch, PoultryMovement
    from warehouse.models import Product, StockTransaction

OWNER_PATH = "/my"
TITLES = {key: definition.title_html for key, definition in registry.EVENTS.items()}

# Backwards compatible names (Phase 1 imports).
_household_rows = household_rows
_qty = qty

__all__ = ["compose", "household_rows", "qty"]


def household_created(household: Household) -> None:
    address = ", ".join(
        part for part in (household.region, household.district, household.mfy, household.address) if part
    )
    rows: list[Row] = [
        *household_rows(household),
        ("Telefon", household.phone),
        ("Manzil", address),
        ("Sana", fmt_date(household.created_at)),
    ]
    text = compose("household_created", rows, "Ro‘yxatga olindi", household.code)
    notify_on_commit(
        "household_created",
        key=household.pk,
        text=text,
        staff_path=f"/households/{household.pk}",
        related_op=household.code,
        object_type="Household",
        object_id=household.pk,
    )


def user_created(user: User, *, notify_admins: bool = True) -> None:
    """New account. Admins get it for staff users; a new owner only gets his own welcome row."""
    rows: list[Row] = [
        ("F.I.Sh.", user.full_name),
        ("Login", user.username),
        ("Rol", role_display(user)),
        ("Telefon", user.phone),
        ("Sana", fmt_date(user.date_joined)),
    ]
    text = compose("user_created", rows, "Yaratildi", user.username)
    notify_on_commit(
        "user_created",
        key=user.pk,
        text=text,
        staff_path="/users",
        related_op=user.username,
        contextual={registry.SUBJECT_USER: user},
        contextual_paths={registry.SUBJECT_USER: "/"},
        include_roles=notify_admins,
        object_type="User",
        object_id=user.pk,
    )


def batch_created(batch: PoultryBatch) -> None:
    rows: list[Row] = [
        ("Partiya", batch.code),
        ("Parranda turi", batch.get_poultry_type_display()),
        ("Zoti", batch.breed),
        ("Miqdori", qty(batch.initial_quantity, "dona")),
        ("Yoshi", f"{fmt_number(batch.age_days)} kun"),
        ("Yetkazib beruvchi", batch.supplier),
        ("Sana", fmt_date(batch.arrived_at)),
    ]
    text = compose("batch_created", rows, "Qabul qilindi", batch.code)
    notify_on_commit(
        "batch_created",
        key=batch.pk,
        text=text,
        staff_path=f"/batches/{batch.pk}",
        related_op=batch.code,
        object_type="PoultryBatch",
        object_id=batch.pk,
    )


_MOVEMENT_EVENTS = {
    MovementType.DISTRIBUTE: ("poultry_distributed", "Tarqatildi"),
    MovementType.RETURN: ("poultry_returned", "Qaytarildi"),
    MovementType.DEATH: ("poultry_death", "Nobud bo‘ldi"),
    MovementType.OUT: ("poultry_out", "Chiqim qilindi"),
}


def movement_recorded(movement: PoultryMovement) -> None:
    event, status = _MOVEMENT_EVENTS[movement.type]
    batch = movement.batch
    household = movement.household
    rows: list[Row] = []
    if household is not None:
        rows.extend(household_rows(household))
    else:
        rows.append(("Joyi", "Markaz"))
    rows.extend(
        [
            ("Parranda turi", batch.get_poultry_type_display()),
            ("Miqdori", qty(movement.quantity, "dona")),
            ("Partiya", batch.code),
            ("Sana", fmt_date(movement.date)),
            ("Izoh", movement.note),
        ]
    )
    text = compose(event, rows, status, movement.op_id)
    batch_path = f"/batches/{batch.pk}"
    staff_path = f"/households/{household.pk}" if household is not None else batch_path
    # poultry_out goes to ADMINs only (Phase 1 table), even when the birds left a household.
    owner_event = registry.HOUSEHOLD_OWNER in registry.get(event).contextual
    contextual = {registry.HOUSEHOLD_OWNER: household.owner} if household is not None and owner_event else {}
    notify_on_commit(
        event,
        key=movement.pk,
        text=text,
        staff_path=staff_path,
        owner_path=OWNER_PATH,
        related_op=movement.op_id,
        contextual=contextual,
        object_type="PoultryMovement",
        object_id=movement.pk,
    )


def stock_recorded(transaction_row: StockTransaction, *, crossed_low: bool, owner_notified: bool = True) -> None:
    """``stock_in`` / ``stock_out``. Task/vaccine/treatment OUTs pass ``owner_notified=False`` (WAREHOUSE only)."""
    product = transaction_row.product
    unit = product.unit
    is_out = transaction_row.type == StockTransactionType.OUT
    event = "stock_out" if is_out else "stock_in"
    rows: list[Row] = [
        ("Mahsulot", product.name),
        ("Toifa", product.get_category_display()),
        ("Miqdori", qty(transaction_row.quantity, unit)),
        ("Qoldiq", qty(transaction_row.balance_after, unit)),
    ]
    household = transaction_row.household
    if household is not None:
        rows.extend(household_rows(household))
    rows.extend(
        [
            ("Hujjat raqami", transaction_row.document_no),
            ("Sana", fmt_date(transaction_row.created_at)),
            ("Izoh", transaction_row.note),
        ]
    )
    text = compose(event, rows, "Chiqim qilindi" if is_out else "Kirim qilindi", transaction_row.op_id)
    contextual = (
        {registry.HOUSEHOLD_OWNER: household.owner} if is_out and household is not None and owner_notified else {}
    )
    notify_on_commit(
        event,
        key=transaction_row.pk,
        text=text,
        staff_path=f"/warehouse/{product.pk}",
        owner_path=OWNER_PATH,
        related_op=transaction_row.op_id,
        contextual=contextual,
        object_type="StockTransaction",
        object_id=transaction_row.pk,
    )
    if crossed_low:
        low_stock(product, transaction_row)


def low_stock(product: Product, transaction_row: StockTransaction) -> None:
    rows: list[Row] = [
        ("Mahsulot", product.name),
        ("Toifa", product.get_category_display()),
        ("Qoldiq", qty(transaction_row.balance_after, product.unit)),
        ("Minimal zaxira", qty(product.min_stock, product.unit)),
        ("Sana", fmt_date(transaction_row.created_at)),
    ]
    text = compose("low_stock", rows, "Minimal zaxiraga yetildi", transaction_row.op_id)
    notify_on_commit(
        "low_stock",
        key=transaction_row.pk,
        text=text,
        staff_path=f"/warehouse/{product.pk}",
        related_op=transaction_row.op_id,
        object_type="Product",
        object_id=product.pk,
    )


def daily_report_created(report: DailyReport) -> None:
    household = report.household
    rows: list[Row] = [
        *household_rows(household),
        ("Sana", fmt_date(report.date)),
        ("Parranda soni", qty(report.bird_count, "dona")),
        ("Go‘sht", qty(report.meat_kg, "kg") if report.meat_kg is not None else None),
        ("Yem", qty(report.feed_kg, "kg")),
        ("Nobud bo‘lgan", qty(report.deaths, "dona")),
        ("O‘rtacha vazn", qty(report.avg_weight_g, "g") if report.avg_weight_g is not None else None),
        ("Izoh", report.note),
    ]
    text = compose("daily_report", rows, "Kiritildi", report.op_id)
    notify_on_commit(
        "daily_report",
        key=report.pk,
        text=text,
        staff_path=f"/households/{household.pk}",
        related_op=report.op_id,
        object_type="DailyReport",
        object_id=report.pk,
    )

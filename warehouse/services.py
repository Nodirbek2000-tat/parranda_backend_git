"""Warehouse business logic. Balances, reservations and lot quantities change only here, under row locks.

Public stock API used by the other modules (API_CONTRACT_V3 §17.1 — nobody else writes ``Product.balance``,
``Product.reserved`` or lot quantities):

- :func:`record_stock_transaction` — manual IN / OUT (Phase 1 endpoint, V3 lots and reservation rules), also
  the OUT of vaccinations / treatments (``owner_notified=False``, ``reference``);
- :func:`lock_products` — lock products in ascending id order (call before several stock operations);
- :func:`post_locked` — IN / OUT on an already locked product (logistics task OUT at ``start`` and the
  return IN on cancel; ``Product.reserved`` itself is kept by ``logistics/stock.py``);
- :func:`apply_adjustment` — inventory ADJUST_IN / ADJUST_OUT (:mod:`warehouse.inventory`).

All of them must run inside ``transaction.atomic()`` (they open one themselves when called alone).
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable
from datetime import date
from decimal import Decimal

from django.db import IntegrityError, transaction
from django.db.models import Sum
from django.utils import timezone
from rest_framework.exceptions import ValidationError

from accounts.models import User
from audit import services as audit
from audit.models import AuditAction, AuditModule
from common.formatting import fmt_number
from common.softdelete import InUse, deleted_label
from farm.models import Household
from notifications import events

from . import events as warehouse_events
from . import norms
from .models import (
    ADJUST_TYPES,
    MAX_AMOUNT,
    OUTGOING_TYPES,
    WHOLE_UNITS,
    Product,
    StockLot,
    StockLotAllocation,
    StockTransaction,
    StockTransactionType,
)

ZERO = Decimal("0")
RUNNING_LOW_FACTOR = Decimal("1.5")

MSG_NOT_ENOUGH = "Omborda yetarli emas. Qoldiq: {balance} {unit}"
MSG_NOT_ENOUGH_RESERVED = (
    "Omborda yetarli emas. Bo‘sh qoldiq: {available} {unit} ({reserved} {unit} yetkazib berish uchun zaxiralangan)"
)
MSG_RESERVE_SHORT = "“{name}”: omborda yetarli emas. Bo‘sh qoldiq: {available} {unit}"
MSG_EXPIRED_IN = "Yaroqlilik muddati o‘tgan mahsulotni kirim qilib bo‘lmaydi."
MSG_LOT_ONLY_IN = "Partiya faqat kirimda ko‘rsatiladi."
MSG_LOT_SHORT = "Tanlangan partiyada yetarli qoldiq yo‘q."
MSG_INACTIVE = "Mahsulot faol emas"


def _whole_units_error(product: Product, quantity: Decimal) -> str | None:
    if product.unit in WHOLE_UNITS and quantity != quantity.to_integral_value():
        return f"“{product.unit}” uchun butun son kiriting"
    return None


def lock_products(product_ids: Iterable[int]) -> dict[int, Product]:
    """``SELECT … FOR UPDATE`` of the products in ascending id order (lock order §17.1)."""
    ids = sorted({int(pk) for pk in product_ids})
    # ``all_objects``: running work (a task cancelled in transit, an inventory) may still touch a deleted product.
    return {
        product.pk: product for product in Product.all_objects.select_for_update().filter(pk__in=ids).order_by("pk")
    }


# --- Posting -----------------------------------------------------------------------------------


def _consume_lots(product: Product, row: StockTransaction, quantity: Decimal, balance_before: Decimal,
                  lot: StockLot | None = None) -> None:
    """Outgoing quantity: untracked stock first, then lots FEFO (expiry asc NULLS LAST, id); §5.2."""
    lots = list(StockLot.objects.select_for_update().filter(product=product, remaining_qty__gt=0).order_by("pk"))
    left = quantity
    taken: list[tuple[StockLot, Decimal]] = []
    if lot is not None:
        chosen = next(item for item in lots if item.pk == lot.pk)
        take = min(chosen.remaining_qty, left)
        taken.append((chosen, take))
        left -= take
        lots = [item for item in lots if item.pk != chosen.pk]
    else:
        untracked = balance_before - sum((item.remaining_qty for item in lots), ZERO)
        left -= min(max(untracked, ZERO), left)
    lots.sort(key=lambda item: (item.expiry_date is None, item.expiry_date or date.max, item.pk))
    for item in lots:
        if left <= 0:
            break
        take = min(item.remaining_qty, left)
        taken.append((item, take))
        left -= take
    for item, take in taken:
        item.remaining_qty -= take
    if taken:
        StockLot.objects.bulk_update([item for item, _ in taken], ["remaining_qty"])
        StockLotAllocation.objects.bulk_create(
            [StockLotAllocation(transaction=row, lot=item, quantity=take) for item, take in taken]
        )


def _post(
    *,
    product: Product,
    actor: User | None,
    transaction_type: str,
    quantity: Decimal,
    document_no: str,
    household: Household | None = None,
    note: str = "",
    lot_no: str = "",
    expiry_date: date | None = None,
    lot: StockLot | None = None,
    reference: tuple[str, int] | None = None,
    owner_notified: bool = True,
) -> StockTransaction:
    """Write one operation on an already locked and validated ``product``."""
    before = product.balance
    is_out = transaction_type in OUTGOING_TYPES
    after = before - quantity if is_out else before + quantity
    product.balance = after
    product.save(update_fields=["balance", "reserved"])
    reference_type, reference_id = reference if reference else ("", None)
    row = StockTransaction.objects.create(
        product=product,
        type=transaction_type,
        quantity=quantity,
        balance_after=after,
        household=household,
        document_no=document_no,
        note=note,
        created_by=actor,
        reference_type=reference_type,
        reference_id=reference_id,
    )
    if is_out:
        _consume_lots(product, row, quantity, before, lot)
    elif transaction_type == StockTransactionType.IN:  # ADJUST_IN stays untracked stock (§5.2)
        StockLot.objects.create(
            product=product,
            lot_no=lot_no,
            expiry_date=expiry_date,
            received_qty=quantity,
            remaining_qty=quantity,
            in_transaction=row,
        )
    audit.record_create(AuditModule.STOCK, row, actor)
    crossed_low = is_out and before > product.min_stock >= after
    if transaction_type in (StockTransactionType.IN, StockTransactionType.OUT):
        events.stock_recorded(row, crossed_low=crossed_low, owner_notified=owner_notified)
    elif crossed_low:  # ADJUST_OUT: only the crossing check (§5.3)
        events.low_stock(product, row)
    threshold = product.min_stock * RUNNING_LOW_FACTOR
    if is_out and product.min_stock > 0 and before > threshold >= after > product.min_stock:
        warehouse_events.stock_running_low(product, row)
    return row


def _out_error(product: Product, quantity: Decimal) -> str | None:
    if quantity <= product.available:
        return None
    if product.reserved > 0:
        return MSG_NOT_ENOUGH_RESERVED.format(
            available=fmt_number(max(product.available, ZERO)),
            reserved=fmt_number(product.reserved),
            unit=product.unit,
        )
    return MSG_NOT_ENOUGH.format(balance=fmt_number(product.balance), unit=product.unit)


# --- Posting for other modules ------------------------------------------------------------------
# ``logistics/stock.py`` keeps ``Product.reserved`` for tasks and posts its OUT / return IN through
# :func:`post_locked`; vet OUTs go through :func:`record_stock_transaction` (``owner_notified=False``).


def post_locked(
    *,
    actor: User | None,
    product: Product,
    transaction_type: str,
    quantity: Decimal,
    document_no: str,
    household: Household | None = None,
    note: str = "",
    notify_owner: bool = True,
    owner_notified: bool | None = None,
    lot_no: str = "",
    expiry_date: date | None = None,
    reference: tuple[str, int] | None = None,
) -> StockTransaction:
    """IN / OUT on a product the caller has ALREADY locked (``select_for_update``) and validated.

    Checks the physical balance (``OUT ≤ balance``; reservations are the caller's business) and the
    column limit for IN. Lots, audit (``stock / create``) and notifications as for a manual operation;
    ``notify_owner=False`` sends ``stock_out`` to WAREHOUSE only (§9.4).
    """
    if transaction_type not in (StockTransactionType.IN, StockTransactionType.OUT):
        raise ValueError(f"post_locked: unsupported type {transaction_type!r}")
    if quantity <= 0:
        raise ValidationError({"quantity": ["Miqdor 0 dan katta bo‘lishi kerak"]})
    if transaction_type == StockTransactionType.OUT and quantity > product.balance:
        raise ValidationError({"quantity": [MSG_NOT_ENOUGH.format(balance=fmt_number(product.balance), unit=product.unit)]})
    if transaction_type == StockTransactionType.IN and product.balance + quantity > MAX_AMOUNT:
        raise ValidationError({"quantity": [f"Qoldiq {fmt_number(MAX_AMOUNT)} {product.unit} dan oshib ketadi"]})
    return _post(
        product=product,
        actor=actor,
        transaction_type=transaction_type,
        quantity=quantity,
        document_no=document_no,
        household=household,
        note=note[:500],
        lot_no=(lot_no or "").strip()[:64],
        expiry_date=expiry_date,
        reference=reference,
        owner_notified=notify_owner if owner_notified is None else owner_notified,
    )


# --- Manual operations and inventory adjustments ------------------------------------------------

MSG_ADJUST_ONLY_INVENTORY = "Bu turdagi operatsiya faqat inventarizatsiya orqali yaratiladi."
MSG_LOT_ONLY_OUT = "Partiyani faqat chiqimda tanlash mumkin."


def record_stock_transaction(
    *,
    actor: User,
    product_id: int,
    transaction_type: str,
    quantity: Decimal,
    document_no: str,
    household: Household | None = None,
    note: str = "",
    lot_no: str = "",
    expiry_date: date | None = None,
    lot_id: int | None = None,
    owner_notified: bool = True,
    reference: tuple[str, int] | None = None,
    file_ids: list[int] | None = None,
) -> StockTransaction:
    """Manual IN / OUT (``POST /api/stock-transactions``, V3 §5.1/§5.2). ADJUST_* only via inventory.

    ``file_ids``: unbound ``STOCK_DOCUMENT`` uploads of the actor (scan of the waybill / invoice) bound to the
    new operation in the same transaction (TZ §7).

    Also used by other modules for their OUTs (vaccination, treatment): ``owner_notified=False`` sends
    ``stock_out`` to WAREHOUSE only, ``reference=("vaccination", id)`` fills the §5.4 reference.
    """
    if transaction_type in ADJUST_TYPES:
        raise ValidationError({"type": [MSG_ADJUST_ONLY_INVENTORY]})
    if household is not None and transaction_type != StockTransactionType.OUT:
        raise ValidationError({"household": ["Xonadon faqat chiqim uchun ko‘rsatiladi"]})
    lot_no = (lot_no or "").strip()
    if transaction_type != StockTransactionType.IN and (lot_no or expiry_date is not None):
        raise ValidationError({"lot_no": [MSG_LOT_ONLY_IN]})  # V3 §5.2: same key for lot_no / expiry_date
    if transaction_type != StockTransactionType.OUT and lot_id is not None:
        raise ValidationError({"lot": [MSG_LOT_ONLY_OUT]})
    if expiry_date is not None and expiry_date < timezone.localdate():
        raise ValidationError({"expiry_date": [MSG_EXPIRED_IN]})

    with transaction.atomic():
        product = Product.objects.select_for_update().get(pk=product_id)
        if not product.is_active:
            raise ValidationError({"product": [MSG_INACTIVE]})
        error = _whole_units_error(product, quantity)
        if error:
            raise ValidationError({"quantity": [error]})
        lot = None
        if transaction_type == StockTransactionType.OUT:
            error = _out_error(product, quantity)
            if error:
                raise ValidationError({"quantity": [error]})
            if lot_id is not None:
                lot = StockLot.objects.select_for_update().filter(pk=lot_id, product_id=product.pk).first()
                if lot is None or lot.remaining_qty < quantity:
                    raise ValidationError({"lot": [MSG_LOT_SHORT]})
        elif product.balance + quantity > MAX_AMOUNT:
            raise ValidationError({"quantity": [f"Qoldiq {fmt_number(MAX_AMOUNT)} {product.unit} dan oshib ketadi"]})
        row = _post(
            product=product,
            actor=actor,
            transaction_type=transaction_type,
            quantity=quantity,
            document_no=document_no,
            household=household,
            note=note,
            lot_no=lot_no[:64],
            expiry_date=expiry_date,
            lot=lot,
            reference=reference,
            owner_notified=owner_notified,
        )
        if file_ids:
            from core import files
            from core.models import AttachmentPurpose

            files.bind_files(file_ids, actor=actor, purpose=AttachmentPurpose.STOCK_DOCUMENT, obj=row)
        return row


def apply_adjustment(
    *,
    actor: User | None,
    product: Product,
    difference: Decimal,
    document_no: str,
    note: str = "",
    count_id: int | None = None,
) -> StockTransaction | None:
    """Inventory adjustment of a LOCKED product (V3 §5.3): ``difference > 0`` → ``ADJUST_IN`` (untracked
    stock, no lot), ``< 0`` → ``ADJUST_OUT`` (consumes untracked first, then lots FEFO), ``0`` → nothing.

    ``count_id`` fills the §5.4 ``reference`` (``{"type": "inventory", "code": "INV-…"}``)."""
    if not difference:
        return None
    if difference > 0 and product.balance + difference > MAX_AMOUNT:
        raise ValidationError({"lines": [f"“{product.name}”: qoldiq juda katta bo‘lib ketadi."]})
    tx_type = StockTransactionType.ADJUST_IN if difference > 0 else StockTransactionType.ADJUST_OUT
    return _post(
        product=product,
        actor=actor,
        transaction_type=tx_type,
        quantity=abs(difference),
        document_no=document_no,
        note=note,
        reference=("inventory", count_id) if count_id is not None else None,
    )


MSG_DUPLICATE_NAME = "Bu nomdagi mahsulot allaqachon mavjud"
PRODUCT_EDITABLE_FIELDS = frozenset(
    {"name", "category", "unit", "min_stock", "is_active", "price", "consumption_norm", "dosage", "description"}
)


def create_product(*, actor: User | None, data: dict) -> Product:
    try:
        with transaction.atomic():
            product = Product.objects.create(**data)
            norms.record_product_created(product, actor)
    except IntegrityError:
        # A concurrent request created the same (case-insensitive) name first.
        raise ValidationError({"name": [MSG_DUPLICATE_NAME]}) from None
    return product


def update_product(*, actor: User | None, product_id: int, changes: dict) -> Product:
    """PATCH of product fields (never ``balance``), under the product row lock."""
    unknown = set(changes) - PRODUCT_EDITABLE_FIELDS
    if unknown:
        raise ValueError(f"Not editable: {sorted(unknown)}")
    try:
        with transaction.atomic():
            product = Product.objects.select_for_update().get(pk=product_id)
            before = norms.product_state(product)
            for field, value in changes.items():
                setattr(product, field, value)
            if changes:
                product.save(update_fields=list(changes))
            norms.record_product_updated(product, actor, before)
    except IntegrityError:
        raise ValidationError({"name": [MSG_DUPLICATE_NAME]}) from None
    return product


MSG_PRODUCT_RESERVED = (
    "Mahsulotning {reserved} {unit} miqdori yetkazib berish uchun zaxiralangan. "
    "Avval tegishli buyurtma va topshiriqlarni yakunlang yoki bekor qiling."
)
MSG_PRODUCT_OPEN_ORDERS = (
    "Mahsulot yakunlanmagan buyurtmalarda bor ({codes}). Avval ularni yakunlang yoki bekor qiling."
)
MSG_PRODUCT_OPEN_TASKS = (
    "Mahsulot yakunlanmagan logistika topshiriqlarida bor ({codes}). Avval ularni yakunlang yoki bekor qiling."
)


def _codes(items: list[str]) -> str:
    shown = ", ".join(items[:3])
    return f"{shown} va yana {len(items) - 3} ta" if len(items) > 3 else shown


def delete_product(*, actor: User | None, product_id: int) -> None:
    """Soft delete a product: hidden from lists, pickers, search and statistics, stock history kept.

    The name gets a ``(o‘chirilgan #id)`` suffix so the same name can be used again. A remaining balance is
    allowed (it simply disappears from the lists); refused (``409``) while stock is reserved or the product is in
    an open order / not-finished logistics task.
    """
    from logistics.models import ACTIVE_STATUSES, LogisticsTaskItem
    from orders.models import OPEN_STATUSES, OrderItem

    with transaction.atomic():
        product = Product.all_objects.select_for_update(of=("self",)).get(pk=product_id)
        if product.deleted_at is not None:
            return
        if product.reserved > 0:
            raise InUse(MSG_PRODUCT_RESERVED.format(reserved=fmt_number(product.reserved), unit=product.unit))
        orders = list(
            OrderItem.objects.filter(product=product, order__status__in=OPEN_STATUSES)
            .order_by("order_id")
            .values_list("order_id", flat=True)
            .distinct()
        )
        if orders:
            raise InUse(MSG_PRODUCT_OPEN_ORDERS.format(codes=_codes([f"BY-{pk:06d}" for pk in orders])))
        tasks = list(
            LogisticsTaskItem.objects.filter(product=product, task__status__in=ACTIVE_STATUSES)
            .order_by("task_id")
            .values_list("task__code", flat=True)
            .distinct()
        )
        if tasks:
            raise InUse(MSG_PRODUCT_OPEN_TASKS.format(codes=_codes([code or "LG" for code in tasks])))
        before = norms.product_state(product)
        product.deleted_at = timezone.now()
        product.is_active = False
        product.name = deleted_label(product.name, product.pk, Product._meta.get_field("name").max_length)
        product.save(update_fields=["deleted_at", "is_active", "name"])
        audit.record(
            module=AuditModule.PRODUCTS,
            action=AuditAction.DELETE,
            obj=product,
            actor=actor,
            before=before,
            after={"deleted": True, "is_active": False, "name": product.name, "balance": str(product.balance)},
        )

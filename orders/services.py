"""Business logic of the orders app (API_CONTRACT_V3 §6).

Every state change: ``transaction.atomic()`` → ``select_for_update()`` on the order (lock order §17.1:
Order → LogisticsTask → Product …) → validate the CURRENT status (``409`` otherwise) → change rows →
status log + ``audit.record`` (same transaction) → :mod:`orders.events` (queued after commit only).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Any

from django.db import transaction
from django.http import Http404
from django.utils import timezone
from rest_framework import status
from rest_framework.exceptions import APIException, ValidationError

from accounts.models import User
from audit import services as audit
from audit.models import AuditAction, AuditModule
from common.formatting import fmt_number
from common.roles import Role, role_of
from farm.models import Household
from warehouse.models import MAX_AMOUNT, WHOLE_UNITS, Product

from . import bridge, events
from .models import MAX_ITEMS, Order, OrderItem, OrderKind, OrderStatus, OrderStatusLog

# --- Messages (API_CONTRACT_V3 §6.4) -----------------------------------------------------------

MSG_ITEMS_EMPTY = "Kamida bitta mahsulot tanlang."
MSG_ITEMS_TOO_MANY = f"Bitta buyurtmada {MAX_ITEMS} tadan ko‘p mahsulot bo‘lmasin."
MSG_ITEMS_DUPLICATE = "Mahsulot takrorlanmasin."
MSG_QTY_POSITIVE = "Miqdor 0 dan katta bo‘lishi kerak"
MSG_DESIRED_DATE_PAST = "Sana o‘tgan kun bo‘lishi mumkin emas."
MSG_HOUSEHOLD_INACTIVE_OWNER = "Xonadoningiz faol emas. Administratorga murojaat qiling."
MSG_HOUSEHOLD_INACTIVE = "Xonadon faol emas"
MSG_HOUSEHOLD_REQUIRED = "Xonadonni tanlang."
MSG_NO_HOUSEHOLD = "Hisobingizga xonadon biriktirilmagan. Administratorga murojaat qiling."
MSG_APPROVE_ALL_ITEMS = "Buyurtmadagi har bir mahsulot uchun miqdorni bir martadan ko‘rsating."
MSG_APPROVE_NONE_POSITIVE = "Kamida bitta mahsulot miqdori 0 dan katta bo‘lsin."
MSG_APPROVE_QTY_NEGATIVE = "Tasdiqlangan miqdor manfiy bo‘lishi mumkin emas."
MSG_DELIVERY_STARTED = "Yetkazib berish boshlangan. Avval logistika topshirig‘ini bekor qiling."
MSG_TASK_EXISTS = "Bu buyurtma uchun faol logistika topshirig‘i allaqachon mavjud."
MSG_REASON_REQUIRED = "Sababni yozing (kamida 3 ta belgi)."


def msg_inactive_product(name: str) -> str:
    return f"“{name}” mahsulot faol emas."


def msg_whole_units(name: str, unit: str) -> str:
    return f"“{name}”: “{unit}” uchun butun son kiriting"


def msg_wrong_status(order: Order) -> str:
    return f"Buyurtma holati: {order.get_status_display()}. Amalni bajarib bo‘lmaydi."


class Conflict(APIException):
    """``409 {"detail": "..."}`` — the current status does not allow the action."""

    status_code = status.HTTP_409_CONFLICT
    default_detail = "Amalni bajarib bo‘lmaydi."
    default_code = "conflict"


# --- Helpers --------------------------------------------------------------------------------


@dataclass(frozen=True)
class ItemInput:
    product: Product
    quantity: Decimal
    note: str = ""


def lock_order(order_id: int) -> Order:
    """Lock and return the order row (first lock of every order/logistics transition)."""
    # of=("self",): PostgreSQL would otherwise also lock the joined household and user rows.
    return Order.objects.select_for_update(of=("self",)).select_related("household__owner").get(pk=order_id)


def _actor_name(actor: User | None) -> str:
    if actor is None:
        return ""
    return (getattr(actor, "full_name", "") or actor.get_username())[:200]


def _log(order: Order, from_status: str, to_status: str, actor: User | None, note: str = "") -> OrderStatusLog:
    return OrderStatusLog.objects.create(
        order=order,
        from_status=from_status,
        to_status=to_status,
        actor=actor,
        actor_name=_actor_name(actor),
        note=(note or "")[:500],
    )


def _audit_status(order: Order, actor: User | None, old: str, note: str = "", extra: dict | None = None) -> None:
    after: dict[str, Any] = {"status": order.status, "note": note}
    if extra:
        after.update(extra)
    audit.record(
        module=AuditModule.ORDERS,
        action=AuditAction.STATUS_CHANGE,
        obj=order,
        actor=actor,
        before={"status": old},
        after=after,
    )


def _transition(order: Order, new_status: str, actor: User | None, note: str = "", *, extra: dict | None = None):
    old = order.status
    order.status = new_status
    order.save()
    _log(order, old, new_status, actor, note)
    _audit_status(order, actor, old, note, extra)


def _check_whole(product: Product, quantity: Decimal) -> None:
    if product.unit in WHOLE_UNITS and quantity != quantity.to_integral_value():
        raise ValidationError({"items": [msg_whole_units(product.name, product.unit)]})


# --- Create ---------------------------------------------------------------------------------


def create_order(
    *,
    actor: User,
    kind: str,
    items: list[ItemInput],
    household: Household | None = None,
    note: str = "",
    desired_date: date | None = None,
) -> Order:
    """``POST /api/orders``: OWNER for his own household, ADMIN for any active household."""
    if kind not in OrderKind.values:
        raise ValidationError({"kind": [f"“{kind}” ruxsat etilgan qiymat emas."]})
    if not items:
        raise ValidationError({"items": [MSG_ITEMS_EMPTY]})
    if len(items) > MAX_ITEMS:
        raise ValidationError({"items": [MSG_ITEMS_TOO_MANY]})
    if len({item.product.pk for item in items}) != len(items):
        raise ValidationError({"items": [MSG_ITEMS_DUPLICATE]})
    if desired_date is not None and desired_date < timezone.localdate():
        raise ValidationError({"desired_date": [MSG_DESIRED_DATE_PAST]})

    is_owner = role_of(actor) == Role.OWNER
    with transaction.atomic():
        if is_owner:
            household = Household.objects.select_related("owner").filter(owner=actor).first()
            if household is None:
                raise ValidationError({"non_field_errors": [MSG_NO_HOUSEHOLD]})
            if not household.is_active:
                raise ValidationError({"non_field_errors": [MSG_HOUSEHOLD_INACTIVE_OWNER]})
        else:
            if household is None:
                raise ValidationError({"household": [MSG_HOUSEHOLD_REQUIRED]})
            household = Household.objects.select_related("owner").get(pk=household.pk)
            if not household.is_active:
                raise ValidationError({"household": [MSG_HOUSEHOLD_INACTIVE]})

        # Re-read the products inside the transaction (active flag / unit may have changed).
        products = Product.objects.in_bulk([item.product.pk for item in items])
        for item in items:
            product = products[item.product.pk]
            if not product.is_active:
                raise ValidationError({"items": [msg_inactive_product(product.name)]})
            if item.quantity <= 0 or item.quantity > MAX_AMOUNT:
                raise ValidationError({"items": [MSG_QTY_POSITIVE]})
            _check_whole(product, item.quantity)

        order = Order.objects.create(
            kind=kind,
            household=household,
            note=note.strip(),
            desired_date=desired_date,
            created_by=actor,
        )
        OrderItem.objects.bulk_create(
            [
                OrderItem(order=order, product=products[item.product.pk], requested_qty=item.quantity, note=item.note)
                for item in items
            ]
        )
        _log(order, "", OrderStatus.NEW, actor)
        audit.record_create(AuditModule.ORDERS, order, actor)
        events.order_created(order)
    return order


# --- Approve / reject -----------------------------------------------------------------------


def _approved_quantities(order: Order, rows: list[dict[str, Any]]) -> dict[int, Decimal]:
    """``{item_id: approved_qty}`` after checking that every item is listed exactly once."""
    items = {item.pk: item for item in order.items.select_related("product")}
    seen: dict[int, Decimal] = {}
    for row in rows:
        item_id, quantity = row["id"], row["approved_qty"]
        if item_id in seen:
            raise ValidationError({"items": [MSG_ITEMS_DUPLICATE]})
        if item_id not in items:
            raise ValidationError({"items": [MSG_APPROVE_ALL_ITEMS]})
        if quantity < 0:
            raise ValidationError({"items": [MSG_APPROVE_QTY_NEGATIVE]})
        if quantity > MAX_AMOUNT:
            raise ValidationError({"items": [f"Miqdor {fmt_number(MAX_AMOUNT)} dan oshmasin."]})
        _check_whole(items[item_id].product, quantity)
        seen[item_id] = quantity
    if set(seen) != set(items):
        raise ValidationError({"items": [MSG_APPROVE_ALL_ITEMS]})
    if not any(quantity > 0 for quantity in seen.values()):
        raise ValidationError({"items": [MSG_APPROVE_NONE_POSITIVE]})
    return seen


def approve_order(
    *,
    actor: User,
    order_id: int,
    items: list[dict[str, Any]],
    decision_note: str = "",
    create_task: bool = True,
    task_data: dict[str, Any] | None = None,
) -> Order:
    """``NEW → APPROVED`` (ADMIN). With ``create_task`` the logistics task is created in the same
    transaction; if stock is short the whole approval fails and nothing is saved."""
    with transaction.atomic():
        order = lock_order(order_id)
        if order.status != OrderStatus.NEW:
            raise Conflict(msg_wrong_status(order))
        quantities = _approved_quantities(order, items)
        if create_task and not bridge.available():
            raise bridge.LogisticsUnavailable()
        order_items = list(order.items.select_related("product").order_by("id"))
        for item in order_items:
            item.approved_qty = quantities[item.pk]
        OrderItem.objects.bulk_update(order_items, ["approved_qty"])

        now = timezone.now()
        order.decided_by = actor
        order.decided_at = now
        order.decision_note = (decision_note or "").strip()
        extra = {
            "items": [
                {"product_id": item.product_id, "requested_qty": item.requested_qty, "approved_qty": item.approved_qty}
                for item in order_items
            ],
            "create_task": bool(create_task),
        }
        _transition(order, OrderStatus.APPROVED, actor, order.decision_note, extra=extra)

        task = None
        if create_task:
            task = bridge.create_task_from_order(
                actor=actor,
                order=order,
                items=[(item, item.approved_qty) for item in order_items if item.approved_qty > 0],
                data=dict(task_data or {}),
            )
        events.order_approved(order, order_items, task)
    return order


def reject_order(*, actor: User, order_id: int, reason: str) -> Order:
    """``NEW → REJECTED`` (ADMIN, final)."""
    reason = (reason or "").strip()
    if len(reason) < 3:
        raise ValidationError({"reason": [MSG_REASON_REQUIRED]})
    with transaction.atomic():
        order = lock_order(order_id)
        if order.status != OrderStatus.NEW:
            raise Conflict(msg_wrong_status(order))
        order.decided_by = actor
        order.decided_at = timezone.now()
        order.decision_note = reason
        _transition(order, OrderStatus.REJECTED, actor, reason)
        events.order_rejected(order)
    return order


# --- Cancel ---------------------------------------------------------------------------------


def cancel_order(*, actor: User, order_id: int, reason: str = "") -> Order:
    """OWNER: own ``NEW`` order (reason optional). ADMIN: ``NEW``/``APPROVED`` (reason 3–500).

    A linked task that has not left the warehouse is cancelled first (reservations released);
    an ``IN_TRANSIT`` or later task → ``409``.
    """
    reason = (reason or "").strip()
    is_owner = role_of(actor) == Role.OWNER
    if not is_owner and len(reason) < 3:
        raise ValidationError({"reason": [MSG_REASON_REQUIRED]})
    with transaction.atomic():
        order = lock_order(order_id)
        if is_owner:
            if order.household.owner_id != actor.pk:  # views already answer 404; defence in depth
                raise Http404
            if order.status != OrderStatus.NEW:
                raise Conflict(msg_wrong_status(order))
        else:
            if order.status in (OrderStatus.IN_DELIVERY, OrderStatus.DELIVERED):
                raise Conflict(MSG_DELIVERY_STARTED)
            if order.status not in (OrderStatus.NEW, OrderStatus.APPROVED):
                raise Conflict(msg_wrong_status(order))

        task = bridge.active_task(order.pk, lock=True)
        if task is not None:
            if task.status in bridge.TASK_STARTED:
                raise Conflict(MSG_DELIVERY_STARTED)
            bridge.cancel_task(actor=actor, task=task, reason=reason or f"{order.code} buyurtmasi bekor qilindi")
            order.refresh_from_db()
            if order.status != OrderStatus.APPROVED:  # pragma: no cover - logistics synced it unexpectedly
                raise Conflict(msg_wrong_status(order))

        order.cancelled_by = actor
        order.cancelled_at = timezone.now()
        order.cancel_reason = reason
        _transition(order, OrderStatus.CANCELLED, actor, reason)
        events.order_cancelled(order, by_owner=is_owner)
    return order


# --- Logistics ------------------------------------------------------------------------------


def create_task_for_order(*, actor: User, order_id: int, data: dict[str, Any]):
    """``POST /api/orders/{id}/create-task`` (ADMIN): APPROVED order without an active task."""
    with transaction.atomic():
        order = lock_order(order_id)
        if order.status != OrderStatus.APPROVED:
            raise Conflict(msg_wrong_status(order))
        if not bridge.available():
            raise bridge.LogisticsUnavailable()
        if bridge.active_task(order.pk, lock=True) is not None:
            raise Conflict(MSG_TASK_EXISTS)
        items = [
            (item, item.approved_qty)
            for item in order.items.select_related("product").order_by("id")
            if item.approved_qty and item.approved_qty > 0
        ]
        if not items:
            raise ValidationError({"items": [MSG_APPROVE_NONE_POSITIVE]})
        return bridge.create_task_from_order(actor=actor, order=order, items=items, data=dict(data))


#: Order status that follows a task status (API_CONTRACT_V3 §6.2). Other task statuses change nothing.
TASK_TO_ORDER = {
    "IN_TRANSIT": OrderStatus.IN_DELIVERY,
    "DELIVERED": OrderStatus.DELIVERED,
    "CONFIRMED": OrderStatus.CONFIRMED,
    "CANCELLED": OrderStatus.APPROVED,
}
_SYNC_FROM = {
    OrderStatus.IN_DELIVERY: {OrderStatus.APPROVED},
    OrderStatus.DELIVERED: {OrderStatus.APPROVED, OrderStatus.IN_DELIVERY},
    OrderStatus.CONFIRMED: {OrderStatus.APPROVED, OrderStatus.IN_DELIVERY, OrderStatus.DELIVERED},
    OrderStatus.APPROVED: {OrderStatus.IN_DELIVERY, OrderStatus.DELIVERED},
}


def sync_order_with_task(*, task, actor: User | None, note: str = "") -> Order | None:
    """Locks the task's order and applies :func:`sync_from_task` (for callers that hold no order lock)."""
    order_id = getattr(task, "order_id", None)
    if not order_id or TASK_TO_ORDER.get(getattr(task, "status", "")) is None:
        return None
    order = lock_order(order_id)
    sync_from_task(order, task, task.status, actor, note)
    return order


def sync_from_task(order: Order, task, status: str, actor: User | None, note: str = "") -> None:
    """``logistics.hooks`` callback: runs inside the logistics transaction with ``order`` already locked
    (the logistics services lock the order before the task, §17.1) after a task status change.

    ``IN_TRANSIT → IN_DELIVERY``, ``DELIVERED → DELIVERED``, ``CONFIRMED → CONFIRMED``,
    ``CANCELLED → APPROVED`` (the task link is gone because the task is cancelled). Idempotent: an order
    already in the target status is left alone; a cancelled task of an order that is being cancelled or
    is still ``APPROVED`` changes nothing. An impossible forward transition raises ``409`` so the
    logistics transition rolls back instead of leaving the two objects inconsistent.
    """
    if order is None:
        return
    target = TASK_TO_ORDER.get(status)
    if target is None:
        return
    if order.status == target:
        return
    if order.status not in _SYNC_FROM[target]:
        if target == OrderStatus.APPROVED:
            return  # task cancelled while the order is NEW/CANCELLED/final: nothing to restore
        raise Conflict(msg_wrong_status(order))

    code = bridge.task_code(task)
    text = f"{code}: {bridge.TASK_STATUS_DISPLAY.get(status, status)}"
    if status == "CONFIRMED" and getattr(task, "received_ok", None) is False:
        issue = (getattr(task, "issue_note", "") or "").strip()
        text = f"{code}: muammo bilan qabul qilindi" + (f" — {issue}" if issue else "")
    if status == "CANCELLED":
        reason = (getattr(task, "cancel_reason", "") or "").strip()
        text = f"{code}: topshiriq bekor qilindi" + (f" — {reason}" if reason else "")
    if note:
        text = f"{text}. {note}"
    _transition(order, target, actor, text[:500], extra={"task_id": task.pk, "task_status": status})


def order_refs(order_ids) -> dict[int, dict[str, Any]]:
    """``logistics.hooks`` callback: ``{id: {"id", "code"}}`` of existing orders (one query)."""
    ids = Order.objects.filter(pk__in=list(order_ids)).values_list("pk", flat=True)
    return {pk: {"id": pk, "code": f"BY-{pk:06d}"} for pk in ids}

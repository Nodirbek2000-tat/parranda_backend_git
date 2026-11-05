"""Business logic of the logistics app (API_CONTRACT_V3 §7).

Every state change: ``transaction.atomic()`` → lock the order (if any) → ``select_for_update()`` on the task →
validate the CURRENT status (``409`` otherwise) and the actor → change rows (stock via :mod:`logistics.stock`) →
status log + ``audit.record`` (same transaction) → order sync → :mod:`logistics.events` (queued after commit).
Lock order (§17.1): Order → LogisticsTask → Products (ascending id) → StockLots (ascending id) → Attachments.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Any

from django.db import IntegrityError, transaction
from django.utils import timezone
from rest_framework import status
from rest_framework.exceptions import APIException, NotFound, PermissionDenied, ValidationError

from accounts.models import User
from audit import services as audit
from audit.models import AuditAction, AuditModule
from common.formatting import fmt_number
from common.roles import Role, role_of
from common.softdelete import InUse
from core import files
from core.models import AttachmentPurpose
from core.settings_service import get_settings
from farm.models import Household
from warehouse.models import MAX_AMOUNT, WHOLE_UNITS, Product, Unit

from . import events, hooks, stock
from .models import (
    ACTIVE_STATUSES,
    WORKING_STATUSES,
    LogisticsTask,
    LogisticsTaskItem,
    LogisticsTaskStatusLog,
    StatusSource,
    TaskStatus,
    Vehicle,
    Driver,
)

ZERO = Decimal("0")
MAX_ITEMS = 20

# --- Messages (API_CONTRACT_V3 §7) ---------------------------------------------------------

MSG_NOT_FOUND = "Topshiriq topilmadi."
MSG_OTHER_WORKER = "Bu topshiriq boshqa xodimga biriktirilgan."
MSG_DRIVER_INACTIVE = "Haydovchi faol emas."
MSG_VEHICLE_INACTIVE = "Transport faol emas."
MSG_STAFF_TOGETHER = "Transport va haydovchini birga tanlang."
MSG_RESPONSIBLE = "Mas’ul faqat faol logistika xodimi bo‘lishi mumkin."
MSG_PROOF_REQUIRED = "Yetkazib berish dalilini (foto yoki imzo) biriktiring."
MSG_ISSUE_REQUIRED = "Muammoni qisqacha yozing."
MSG_ADMIN_CONFIRM_REASON = "Egasi nomidan tasdiqlash sababini yozing."
MSG_REASON_REQUIRED = "Sababni yozing (kamida 3 ta belgi)."
MSG_HOUSEHOLD_INACTIVE = "Xonadon faol emas"
MSG_ITEMS_EMPTY = "Kamida bitta mahsulot tanlang."
MSG_ITEMS_TOO_MANY = f"Bitta topshiriqda {MAX_ITEMS} tadan ko‘p mahsulot bo‘lmasin."
MSG_ITEMS_DUPLICATE = "Mahsulot takrorlanmasin."
MSG_QTY_POSITIVE = "Miqdor 0 dan katta bo‘lishi kerak"
MSG_PLANNED_PAST = "Sana o‘tgan kun bo‘lishi mumkin emas."
MSG_RECEIVED_REQUIRED = "Qabul qilinganini belgilang."
MSG_PLATE_DUPLICATE = "Bu davlat raqami allaqachon ro‘yxatda."
MSG_DRIVER_USER = "Faqat faol logistika xodimini bog‘lash mumkin."
MSG_DRIVER_USER_TAKEN = "Bu foydalanuvchi boshqa haydovchiga bog‘langan."


def msg_wrong_status(task: LogisticsTask) -> str:
    return f"Topshiriq holati: {task.get_status_display()}. Bu amalni bajarib bo‘lmaydi."


def msg_capacity(total: Decimal, capacity: Decimal) -> str:
    return f"Yuk transport sig‘imidan oshadi: {fmt_number(total)} kg > {fmt_number(capacity)} kg."


def msg_inactive_product(name: str) -> str:
    return f"“{name}” mahsulot faol emas."


def msg_whole_units(name: str, unit: str) -> str:
    return f"“{name}”: “{unit}” uchun butun son kiriting"


class Conflict(APIException):
    """``409 {"detail": "..."}`` — the current status does not allow the action."""

    status_code = status.HTTP_409_CONFLICT
    default_detail = "Amalni bajarib bo‘lmaydi."
    default_code = "conflict"


# --- Status machine ------------------------------------------------------------------------

#: action → (allowed current statuses, roles that may run it)
ACTIONS: dict[str, tuple[frozenset[str], frozenset[str]]] = {
    "edit": (frozenset({TaskStatus.NEW, TaskStatus.ASSIGNED, TaskStatus.ACCEPTED}), frozenset({Role.ADMIN})),
    "assign": (
        frozenset({TaskStatus.NEW, TaskStatus.ASSIGNED, TaskStatus.ACCEPTED}),
        frozenset({Role.ADMIN, Role.LOGISTICS}),
    ),
    "accept": (frozenset({TaskStatus.ASSIGNED}), frozenset({Role.ADMIN, Role.LOGISTICS})),
    "start": (frozenset({TaskStatus.ACCEPTED}), frozenset({Role.ADMIN, Role.LOGISTICS})),
    "deliver": (frozenset({TaskStatus.IN_TRANSIT}), frozenset({Role.ADMIN, Role.LOGISTICS})),
    "confirm": (frozenset({TaskStatus.DELIVERED}), frozenset({Role.ADMIN, Role.OWNER})),
    "cancel": (
        frozenset({TaskStatus.NEW, TaskStatus.ASSIGNED, TaskStatus.ACCEPTED, TaskStatus.IN_TRANSIT}),
        frozenset({Role.ADMIN}),
    ),
}
ACTION_ORDER = ("assign", "accept", "start", "deliver", "confirm", "cancel", "edit")
#: Actions a LOGISTICS user may run only on own tasks (responsible or driver user).
WORKER_ACTIONS = frozenset({"accept", "start", "deliver"})


def is_worker(task: LogisticsTask, user: User) -> bool:
    """The user is the task's responsible or the user linked to its driver."""
    if task.responsible_id == user.pk:
        return True
    driver = task.driver
    return driver is not None and driver.user_id is not None and driver.user_id == user.pk


def _worker_allowed(task: LogisticsTask, user: User, action: str) -> bool:
    if is_worker(task, user):
        return True
    # Nobody is responsible yet: the accepting logistics worker becomes responsible (§7.5).
    return action == "accept" and task.responsible_id is None


def allowed_actions(task: LogisticsTask, user: User) -> list[str]:
    """Actions the UI may show for ``user`` (§7.3); the services re-check everything."""
    role = role_of(user)
    result = []
    for action in ACTION_ORDER:
        statuses, roles = ACTIONS[action]
        if task.status not in statuses or role not in roles:
            continue
        if role == Role.LOGISTICS and action in WORKER_ACTIONS and not _worker_allowed(task, user, action):
            continue
        if role == Role.OWNER and task.household.owner_id != user.pk:
            continue
        result.append(action)
    return result


def _check_action(task: LogisticsTask, user: User, action: str) -> None:
    statuses, roles = ACTIONS[action]
    role = role_of(user)
    if role not in roles:
        raise PermissionDenied("Sizda bu amal uchun ruxsat yo‘q.")
    if task.status not in statuses:
        raise Conflict(msg_wrong_status(task))
    if role == Role.LOGISTICS and action in WORKER_ACTIONS and not _worker_allowed(task, user, action):
        raise PermissionDenied(MSG_OTHER_WORKER)


# --- Helpers -------------------------------------------------------------------------------


def _actor_name(actor: User | None) -> str:
    return (actor.full_name if actor is not None else "")[:150]


def _log(
    task: LogisticsTask,
    from_status: str,
    actor: User | None,
    *,
    source: str = StatusSource.API,
    note: str = "",
    latitude: Decimal | None = None,
    longitude: Decimal | None = None,
) -> LogisticsTaskStatusLog:
    return LogisticsTaskStatusLog.objects.create(
        task=task,
        from_status=from_status,
        to_status=task.status,
        actor=actor,
        actor_name=_actor_name(actor),
        source=source,
        note=(note or "")[:1000],
        latitude=latitude,
        longitude=longitude,
    )


def _audit_status(task: LogisticsTask, actor: User | None, old: str, **after: Any) -> None:
    audit.record(
        module=AuditModule.LOGISTICS,
        action=AuditAction.STATUS_CHANGE,
        obj=task,
        actor=actor,
        before={"status": old},
        after={"status": task.status, **{key: value for key, value in after.items() if value not in (None, "")}},
    )


def visible_tasks(user: User):
    """Queryset scope of the list/detail endpoints: OWNER → own household only, staff → all."""
    qs = LogisticsTask.objects.all()
    if role_of(user) == Role.OWNER:
        qs = qs.filter(household__owner_id=user.pk)
    return qs


def _lock_task(task_id: int, user: User | None) -> LogisticsTask:
    """Order lock (if the task has one) → task lock. Invisible task → ``404``."""
    scope = visible_tasks(user) if user is not None else LogisticsTask.objects.all()
    row = scope.filter(pk=task_id).values("order_id").first()
    if row is None:
        raise NotFound(MSG_NOT_FOUND)
    hooks.lock_order(row["order_id"])
    return (
        LogisticsTask.objects.select_for_update(of=("self",))
        .select_related("household__owner", "vehicle", "driver__user", "responsible")
        .get(pk=task_id)
    )


def _snapshot_delivery(task: LogisticsTask, household: Household) -> None:
    parts = [household.region, household.district, household.mfy, household.address]
    task.address_text = ", ".join(part.strip() for part in parts if part and part.strip())[:700]
    task.latitude = household.latitude
    task.longitude = household.longitude
    task.contact_name = household.owner.full_name[:200]
    task.contact_phone = (household.phone or household.owner.phone or "")[:32]


def total_kg(items: list[tuple[Product, Decimal]]) -> Decimal:
    return sum((quantity for product, quantity in items if product.unit == Unit.KG), ZERO)


def task_total_kg(task: LogisticsTask) -> Decimal:
    return total_kg([(item.product, item.quantity) for item in task.items.select_related("product")])


@dataclass(frozen=True)
class ItemInput:
    product: Product
    quantity: Decimal
    order_item_id: int | None = None


def _validate_items(items: list[ItemInput]) -> dict[int, Product]:
    """§7.5 item rules (same as orders): 1–20, distinct, active, ``quantity > 0``, whole units."""
    if not items:
        raise ValidationError({"items": [MSG_ITEMS_EMPTY]})
    if len(items) > MAX_ITEMS:
        raise ValidationError({"items": [MSG_ITEMS_TOO_MANY]})
    if len({item.product.pk for item in items}) != len(items):
        raise ValidationError({"items": [MSG_ITEMS_DUPLICATE]})
    products = Product.objects.in_bulk([item.product.pk for item in items])
    for item in items:
        product = products[item.product.pk]
        if item.quantity <= 0 or item.quantity > MAX_AMOUNT:
            raise ValidationError({"items": [MSG_QTY_POSITIVE]})
        if product.unit in WHOLE_UNITS and item.quantity != item.quantity.to_integral_value():
            raise ValidationError({"items": [msg_whole_units(product.name, product.unit)]})
    return products


def _resolve_staff(
    *,
    vehicle_id: int | None,
    driver_id: int | None,
    responsible_id: int | None,
    actor: User,
    current_responsible: User | None = None,
    kg: Decimal,
) -> tuple[Vehicle, Driver, User | None]:
    """Validate vehicle + driver (+ responsible) for an assignment (§7.5 ``assign``)."""
    if vehicle_id is None or driver_id is None:
        raise ValidationError({"driver" if vehicle_id is not None else "vehicle": [MSG_STAFF_TOGETHER]})
    vehicle = Vehicle.objects.filter(pk=vehicle_id).first()
    if vehicle is None or not vehicle.is_active:
        raise ValidationError({"vehicle": [MSG_VEHICLE_INACTIVE]})
    driver = Driver.objects.select_related("user").filter(pk=driver_id).first()
    if driver is None or not driver.is_active:
        raise ValidationError({"driver": [MSG_DRIVER_INACTIVE]})
    if vehicle.capacity_kg is not None and kg > vehicle.capacity_kg:
        raise ValidationError({"vehicle": [msg_capacity(kg, vehicle.capacity_kg)]})

    def active_logistics(user: User | None) -> bool:
        return user is not None and user.is_active and role_of(user) == Role.LOGISTICS

    if responsible_id is not None:
        responsible = User.objects.filter(pk=responsible_id).first()
        if not active_logistics(responsible):
            raise ValidationError({"responsible": [MSG_RESPONSIBLE]})
    elif active_logistics(driver.user):
        responsible = driver.user
    elif role_of(actor) == Role.LOGISTICS:
        responsible = actor
    elif active_logistics(current_responsible):
        responsible = current_responsible
    else:
        responsible = None
    return vehicle, driver, responsible


def _validate_planned(planned_date: date | None) -> None:
    if planned_date is not None and planned_date < timezone.localdate():
        raise ValidationError({"planned_date": [MSG_PLANNED_PAST]})


# --- Create ------------------------------------------------------------------------------------


def _create(
    *,
    actor: User,
    household: Household,
    items: list[ItemInput],
    planned_date: date | None,
    note: str,
    vehicle_id: int | None,
    driver_id: int | None,
    responsible_id: int | None,
    order_id: int | None = None,
) -> LogisticsTask:
    """Inside the caller's transaction (order already locked when ``order_id`` is given)."""
    products = _validate_items(items)
    staffed = vehicle_id is not None or driver_id is not None
    if responsible_id is not None and not staffed:
        responsible = User.objects.filter(pk=responsible_id).first()
        if responsible is None or not responsible.is_active or role_of(responsible) != Role.LOGISTICS:
            raise ValidationError({"responsible": [MSG_RESPONSIBLE]})
    else:
        responsible = None
    vehicle = driver = None
    if staffed:
        kg = total_kg([(products[item.product.pk], item.quantity) for item in items])
        vehicle, driver, responsible = _resolve_staff(
            vehicle_id=vehicle_id, driver_id=driver_id, responsible_id=responsible_id, actor=actor, kg=kg
        )

    now = timezone.now()
    task = LogisticsTask(
        order_id=order_id,
        household=household,
        planned_date=planned_date,
        note=(note or "").strip()[:500],
        created_by=actor,
        responsible=responsible,
        status=TaskStatus.NEW,
    )
    _snapshot_delivery(task, household)
    if staffed:
        task.status = TaskStatus.ASSIGNED
        task.vehicle, task.driver = vehicle, driver
        task.assigned_at = now
        task.assign_seq = 1
    try:
        with transaction.atomic():
            task.save()
    except IntegrityError:  # one active task per order (concurrent create)
        raise Conflict("Bu buyurtma uchun faol logistika topshirig‘i allaqachon mavjud.") from None
    task.code = LogisticsTask.code_for(task.pk)
    task.save(update_fields=["code"])

    locked = stock.reserve([(item.product.pk, item.quantity) for item in items])
    LogisticsTaskItem.objects.bulk_create(
        [
            LogisticsTaskItem(
                task=task,
                product=locked[item.product.pk],
                quantity=item.quantity,
                reserved_qty=item.quantity,
                order_item_id=item.order_item_id,
            )
            for item in items
        ]
    )
    _log(task, "", actor, note=task.note)
    audit.record_create(AuditModule.LOGISTICS, task, actor)
    events.task_created(task)
    if staffed:
        events.driver_assigned(task)
    return task


def _check_active(household: Household, items: list[ItemInput]) -> None:
    """Household and every product must still be active — for a direct task and for an order approval alike."""
    if not household.is_active:
        raise ValidationError({"household": [MSG_HOUSEHOLD_INACTIVE]})
    products = Product.objects.in_bulk([item.product.pk for item in items])
    for item in items:
        if not products[item.product.pk].is_active:
            raise ValidationError({"items": [msg_inactive_product(products[item.product.pk].name)]})


def create_task(
    *,
    actor: User,
    household: Household,
    items: list[ItemInput],
    planned_date: date | None = None,
    note: str = "",
    vehicle: int | None = None,
    driver: int | None = None,
    responsible: int | None = None,
) -> LogisticsTask:
    """``POST /api/logistics/tasks`` (ADMIN): a delivery without an order."""
    _validate_planned(planned_date)
    with transaction.atomic():
        household = Household.objects.select_related("owner").get(pk=household.pk)
        _check_active(household, items)
        return _create(
            actor=actor,
            household=household,
            items=items,
            planned_date=planned_date,
            note=note,
            vehicle_id=vehicle,
            driver_id=driver,
            responsible_id=responsible,
        )


def _id(value: Any) -> int | None:
    if value is None:
        return None
    return value.pk if hasattr(value, "pk") else int(value)


def create_task_from_order(*, actor: User, order, items, data: dict[str, Any]) -> LogisticsTask:
    """Called by ``orders/bridge.py`` inside the orders transaction, the order row already locked.

    ``items`` = ``[(OrderItem, Decimal quantity)]``; ``data`` = optional ``planned_date``, ``vehicle``,
    ``driver``, ``responsible`` (ids or objects) and ``note``. Does NOT change the order status.
    """
    planned_date = data.get("planned_date")
    if planned_date is None and order.desired_date and order.desired_date >= timezone.localdate():
        planned_date = order.desired_date
    _validate_planned(planned_date)
    household = Household.objects.select_related("owner").get(pk=order.household_id)
    inputs = [
        ItemInput(product=order_item.product, quantity=Decimal(quantity), order_item_id=order_item.pk)
        for order_item, quantity in items
        if quantity and quantity > 0
    ]
    _check_active(household, inputs)  # deactivated after the order was placed → 400, the approval rolls back
    with transaction.atomic():
        return _create(
            actor=actor,
            household=household,
            items=inputs,
            planned_date=planned_date,
            note=data.get("note") or "",
            vehicle_id=_id(data.get("vehicle")),
            driver_id=_id(data.get("driver")),
            responsible_id=_id(data.get("responsible")),
            order_id=order.pk,
        )


# --- Edit ----------------------------------------------------------------------------------------


def update_task(*, actor: User, task_id: int, changes: dict[str, Any]) -> LogisticsTask:
    """``PATCH /api/logistics/tasks/{id}`` (ADMIN): only ``planned_date`` and ``note``."""
    if "planned_date" in changes:
        _validate_planned(changes["planned_date"])
    with transaction.atomic():
        task = _lock_task(task_id, actor)
        _check_action(task, actor, "edit")
        before = audit.snapshot(task)
        fields = []
        if "planned_date" in changes:
            task.planned_date = changes["planned_date"]
            fields.append("planned_date")
        if "note" in changes:
            task.note = (changes["note"] or "").strip()[:500]
            fields.append("note")
        if fields:
            task.save(update_fields=[*fields, "updated_at"])
            audit.record_update(AuditModule.LOGISTICS, task, actor, before)
    return task


# --- Transitions -------------------------------------------------------------------------------


def assign(
    *,
    actor: User,
    task_id: int,
    vehicle: int | None,
    driver: int | None,
    responsible: int | None = None,
    source: str = StatusSource.API,
) -> LogisticsTask:
    """``NEW/ASSIGNED/ACCEPTED → ASSIGNED`` (A, L). Every assignment increments ``assign_seq``."""
    with transaction.atomic():
        task = _lock_task(task_id, actor)
        _check_action(task, actor, "assign")
        vehicle_obj, driver_obj, responsible_obj = _resolve_staff(
            vehicle_id=vehicle,
            driver_id=driver,
            responsible_id=responsible,
            actor=actor,
            current_responsible=task.responsible,
            kg=task_total_kg(task),
        )
        old = task.status
        task.vehicle, task.driver, task.responsible = vehicle_obj, driver_obj, responsible_obj
        task.status = TaskStatus.ASSIGNED
        task.assigned_at = timezone.now()
        task.accepted_at = None
        task.assign_seq += 1
        task.save()
        note = f"{vehicle_obj.plate_number}, {driver_obj.full_name}"
        _log(task, old, actor, source=source, note=note)
        _audit_status(
            task,
            actor,
            old,
            vehicle_id=vehicle_obj.pk,
            driver_id=driver_obj.pk,
            responsible_id=responsible_obj.pk if responsible_obj else None,
        )
        events.driver_assigned(task)
    return task


def accept(*, actor: User, task_id: int, note: str = "", source: str = StatusSource.API) -> LogisticsTask:
    """``ASSIGNED → ACCEPTED`` (L: responsible or driver user; nobody responsible → the actor becomes it)."""
    with transaction.atomic():
        task = _lock_task(task_id, actor)
        _check_action(task, actor, "accept")
        old = task.status
        if task.responsible_id is None and role_of(actor) == Role.LOGISTICS:
            task.responsible = actor
        task.status = TaskStatus.ACCEPTED
        task.accepted_at = timezone.now()
        task.save()
        _log(task, old, actor, source=source, note=note)
        _audit_status(task, actor, old, note=note, responsible_id=task.responsible_id)
    return task


def start(
    *,
    actor: User,
    task_id: int,
    latitude: Decimal | None = None,
    longitude: Decimal | None = None,
    note: str = "",
    source: str = StatusSource.API,
) -> LogisticsTask:
    """``ACCEPTED → IN_TRANSIT``: the stock OUT of every item, exactly once (§7.4)."""
    with transaction.atomic():
        task = _lock_task(task_id, actor)
        _check_action(task, actor, "start")
        items = list(task.items.order_by("product_id"))
        products = stock.lock_products(item.product_id for item in items)
        for item in items:
            if item.stock_transaction_id is not None:  # defensive: the status check already prevents it
                raise Conflict(msg_wrong_status(task))
            row = stock.post_out(
                actor=actor,
                product=products[item.product_id],
                quantity=item.quantity,
                release_qty=item.reserved_qty,
                task=task,
            )
            item.reserved_qty = ZERO
            item.stock_transaction = row
            item.save(update_fields=["reserved_qty", "stock_transaction"])
        old = task.status
        task.status = TaskStatus.IN_TRANSIT
        task.started_at = timezone.now()
        task.save()
        _log(task, old, actor, source=source, note=note, latitude=latitude, longitude=longitude)
        _audit_status(task, actor, old, note=note, out_ids=[item.stock_transaction_id for item in items])
        hooks.sync_order(task, actor, note)
        events.delivery_started(task)
    return task


def deliver(
    *,
    actor: User,
    task_id: int,
    file_ids: list[int] | None = None,
    recipient_name: str = "",
    note: str = "",
    latitude: Decimal | None = None,
    longitude: Decimal | None = None,
    source: str = StatusSource.API,
) -> LogisticsTask:
    """``IN_TRANSIT → DELIVERED`` with proof files (required when ``delivery_proof_required``)."""
    file_ids = list(file_ids or [])
    proof_required = get_settings().delivery_proof_required
    with transaction.atomic():
        task = _lock_task(task_id, actor)
        _check_action(task, actor, "deliver")
        if proof_required and not file_ids:
            raise ValidationError({"file_ids": [MSG_PROOF_REQUIRED]})
        old = task.status
        task.status = TaskStatus.DELIVERED
        task.delivered_at = timezone.now()
        task.recipient_name = (recipient_name or "").strip()[:200]
        task.delivery_note = (note or "").strip()[:500]
        task.save()
        bound = files.bind_files(file_ids, actor=actor, purpose=AttachmentPurpose.DELIVERY_PROOF, obj=task)
        _log(task, old, actor, source=source, note=task.delivery_note, latitude=latitude, longitude=longitude)
        _audit_status(
            task,
            actor,
            old,
            note=task.delivery_note,
            recipient_name=task.recipient_name,
            file_ids=[row.pk for row in bound] or None,
        )
        hooks.sync_order(task, actor, task.delivery_note)
        events.delivery_delivered(task)
    return task


def confirm(
    *,
    actor: User,
    task_id: int,
    received_ok: bool | None,
    issue_note: str = "",
    source: str = StatusSource.API,
) -> LogisticsTask:
    """``DELIVERED → CONFIRMED`` by the owner (or ADMIN on the owner's behalf, with a reason).

    Scope (``404``) and status (``409``) are checked before the body, so another household's owner never learns
    from a ``400`` that the task exists."""
    issue_note = (issue_note or "").strip()
    with transaction.atomic():
        task = _lock_task(task_id, actor)
        _check_action(task, actor, "confirm")
        if received_ok is None:
            raise ValidationError({"received_ok": [MSG_RECEIVED_REQUIRED]})
        if received_ok is False and len(issue_note) < 3:
            raise ValidationError({"issue_note": [MSG_ISSUE_REQUIRED]})
        if role_of(actor) == Role.ADMIN and len(issue_note) < 3:
            raise ValidationError({"issue_note": [MSG_ADMIN_CONFIRM_REASON]})
        old = task.status
        task.status = TaskStatus.CONFIRMED
        task.confirmed_at = timezone.now()
        task.received_ok = bool(received_ok)
        task.issue_note = issue_note[:1000]
        task.save()
        note = issue_note if received_ok else f"Muammo: {issue_note}"
        _log(task, old, actor, source=source, note=note)
        _audit_status(task, actor, old, note=issue_note, received_ok=bool(received_ok))
        hooks.sync_order(task, actor)
        events.delivery_confirmed(task)
    return task


def cancel_task(
    *, actor: User, task_id: int, reason: str, source: str = StatusSource.API
) -> LogisticsTask:
    """``NEW/ASSIGNED/ACCEPTED/IN_TRANSIT → CANCELLED`` (ADMIN). Before the start the reservations are
    released; in transit the goods come back with one ``IN`` per item (set once). A linked order goes back
    to ``APPROVED`` (also called by ``orders/bridge.py`` when the order itself is cancelled)."""
    reason = (reason or "").strip()
    if len(reason) < 3:
        raise ValidationError({"reason": [MSG_REASON_REQUIRED]})
    with transaction.atomic():
        task = _lock_task(task_id, actor)
        _check_action(task, actor, "cancel")
        old = task.status
        items = list(task.items.order_by("product_id"))
        products = stock.lock_products(item.product_id for item in items)
        returned: list[int] = []
        for item in items:
            if item.reserved_qty > 0:
                stock.release([(item.product_id, item.reserved_qty)], products)
                item.reserved_qty = ZERO
                item.save(update_fields=["reserved_qty"])
            if item.stock_transaction_id is not None and item.return_transaction_id is None:
                row = stock.post_return(actor=actor, product=products[item.product_id], quantity=item.quantity,
                                        task=task, out_transaction_id=item.stock_transaction_id)
                item.return_transaction = row
                item.save(update_fields=["return_transaction"])
                returned.append(row.pk)
        task.status = TaskStatus.CANCELLED
        task.cancelled_at = timezone.now()
        task.cancel_reason = reason[:500]
        task.save()
        _log(task, old, actor, source=source, note=reason)
        _audit_status(task, actor, old, note=reason, return_ids=returned or None)
        hooks.sync_order(task, actor)
        events.task_cancelled(task, old_status=old)
    return task


MSG_TASK_WORKING = (
    "Topshiriq ishda (holati: {status}) — haydovchi uni bajarmoqda. Avval topshiriqni bekor qiling, "
    "keyin o‘chirish mumkin."
)
MSG_TASK_AWAITS_CONFIRMATION = (
    "Topshiriq yetkazilgan, {order} buyurtmasi egasining tasdig‘ini kutmoqda. Avval qabul qilinganini "
    "tasdiqlang, keyin o‘chirish mumkin."
)
MSG_TASK_DELETED_REASON = "Topshiriq o‘chirildi"


def delete_task(*, actor: User, task_id: int) -> None:
    """Soft delete a task (``DELETE /api/logistics/tasks/{id}``, ADMIN like cancel): hidden from the board,
    lists, search, summary, the bot and pickers; its items, stock operations, status log and audit stay.

    - ``ASSIGNED / ACCEPTED / IN_TRANSIT`` (a driver is working on it) → ``409``: cancel it first;
    - ``DELIVERED`` of an order (the order waits for the owner's confirmation) → ``409``;
    - ``NEW`` → cancelled first in the same transaction (reservations released, the order goes back to
      ``APPROVED``), then hidden;
    - ``CANCELLED / CONFIRMED`` and an order-less ``DELIVERED`` → hidden as they are.
    """
    with transaction.atomic():
        task = _lock_task(task_id, actor)  # order lock → task lock (§17.1); invisible / deleted → 404
        if role_of(actor) != Role.ADMIN:
            raise PermissionDenied("Sizda bu amal uchun ruxsat yo‘q.")
        if task.status in WORKING_STATUSES:
            raise Conflict(MSG_TASK_WORKING.format(status=task.get_status_display()))
        if task.status == TaskStatus.DELIVERED and task.order_id:
            raise Conflict(MSG_TASK_AWAITS_CONFIRMATION.format(order=hooks.order_code(task.order_id)))
        before = audit.snapshot(task)
        if task.status == TaskStatus.NEW:
            cancel_task(actor=actor, task_id=task.pk, reason=MSG_TASK_DELETED_REASON)
            task.refresh_from_db()
        task.deleted_at = timezone.now()
        task.save(update_fields=["deleted_at", "updated_at"])
        audit.record(
            module=AuditModule.LOGISTICS,
            action=AuditAction.DELETE,
            obj=task,
            actor=actor,
            before=before,
            after={"deleted": True, "status": task.status},
        )


# --- Vehicles and drivers (§7.1, §7.2) ----------------------------------------------------------


def create_vehicle(*, actor: User, data: dict[str, Any]) -> Vehicle:
    try:
        with transaction.atomic():
            vehicle = Vehicle.objects.create(**data)
            audit.record_create(AuditModule.FLEET, vehicle, actor)
    except IntegrityError:
        raise ValidationError({"plate_number": [MSG_PLATE_DUPLICATE]}) from None
    return vehicle


def update_vehicle(*, actor: User, vehicle_id: int, changes: dict[str, Any]) -> Vehicle:
    try:
        with transaction.atomic():
            vehicle = Vehicle.objects.select_for_update().get(pk=vehicle_id)
            before = audit.snapshot(vehicle)
            for field, value in changes.items():
                setattr(vehicle, field, value)
            if changes:
                vehicle.save(update_fields=list(changes))
            audit.record_update(AuditModule.FLEET, vehicle, actor, before)
    except IntegrityError:
        raise ValidationError({"plate_number": [MSG_PLATE_DUPLICATE]}) from None
    return vehicle


def _check_driver_user(user: User | None, driver_id: int | None) -> None:
    if user is None:
        return
    if not user.is_active or role_of(user) != Role.LOGISTICS:
        raise ValidationError({"user": [MSG_DRIVER_USER]})
    if Driver.objects.filter(user=user).exclude(pk=driver_id).exists():
        raise ValidationError({"user": [MSG_DRIVER_USER_TAKEN]})


def create_driver(*, actor: User, data: dict[str, Any]) -> Driver:
    _check_driver_user(data.get("user"), None)
    try:
        with transaction.atomic():
            driver = Driver.objects.create(**data)
            audit.record_create(AuditModule.FLEET, driver, actor)
    except IntegrityError:
        raise ValidationError({"user": [MSG_DRIVER_USER_TAKEN]}) from None
    return driver


def update_driver(*, actor: User, driver_id: int, changes: dict[str, Any]) -> Driver:
    if "user" in changes:
        _check_driver_user(changes["user"], driver_id)
    try:
        with transaction.atomic():
            driver = Driver.objects.select_for_update().get(pk=driver_id)
            before = audit.snapshot(driver)
            for field, value in changes.items():
                setattr(driver, field, value)
            if changes:
                driver.save(update_fields=list(changes))
            audit.record_update(AuditModule.FLEET, driver, actor, before)
    except IntegrityError:
        raise ValidationError({"user": [MSG_DRIVER_USER_TAKEN]}) from None
    return driver


MSG_VEHICLE_IN_USE = (
    "Transport yakunlanmagan logistika topshiriqlariga biriktirilgan ({codes}). "
    "Avval ularni yakunlang, bekor qiling yoki boshqa transport biriktiring."
)
MSG_DRIVER_IN_USE = (
    "Haydovchi yakunlanmagan logistika topshiriqlariga biriktirilgan ({codes}). "
    "Avval ularni yakunlang, bekor qiling yoki boshqa haydovchi biriktiring."
)
DELETED_PLATE_PREFIX = "DEL-"


def _open_task_codes(**lookup) -> str:
    codes = list(
        LogisticsTask.objects.filter(status__in=ACTIVE_STATUSES, **lookup).order_by("id").values_list("pk", flat=True)
    )
    shown = ", ".join(LogisticsTask.code_for(pk) for pk in codes[:3])
    return f"{shown} va yana {len(codes) - 3} ta" if len(codes) > 3 else shown


def delete_vehicle(*, actor: User, vehicle_id: int) -> None:
    """Soft delete a vehicle (task history keeps it). The plate becomes ``DEL-<id>-<plate>`` so the same plate can
    be registered again; refused (``409``) while the vehicle is on a not-finished task."""
    with transaction.atomic():
        vehicle = Vehicle.all_objects.select_for_update(of=("self",)).get(pk=vehicle_id)
        if vehicle.deleted_at is not None:
            return
        codes = _open_task_codes(vehicle=vehicle)
        if codes:
            raise InUse(MSG_VEHICLE_IN_USE.format(codes=codes))
        before = audit.snapshot(vehicle)
        vehicle.deleted_at = timezone.now()
        vehicle.is_active = False
        max_length = Vehicle._meta.get_field("plate_number").max_length
        vehicle.plate_number = f"{DELETED_PLATE_PREFIX}{vehicle.pk}-{vehicle.plate_number}"[:max_length]
        vehicle.save(update_fields=["deleted_at", "is_active", "plate_number"])
        audit.record(
            module=AuditModule.FLEET,
            action=AuditAction.DELETE,
            obj=vehicle,
            actor=actor,
            before=before,
            after={"deleted": True, "is_active": False, "plate_number": vehicle.plate_number},
        )


def delete_driver(*, actor: User, driver_id: int) -> None:
    """Soft delete a driver (task history keeps the name). The linked logistics user is released so it can be
    linked to another driver; refused (``409``) while the driver is on a not-finished task."""
    with transaction.atomic():
        driver = Driver.all_objects.select_for_update(of=("self",)).get(pk=driver_id)
        if driver.deleted_at is not None:
            return
        codes = _open_task_codes(driver=driver)
        if codes:
            raise InUse(MSG_DRIVER_IN_USE.format(codes=codes))
        before = audit.snapshot(driver)
        driver.deleted_at = timezone.now()
        driver.is_active = False
        driver.user = None
        driver.save(update_fields=["deleted_at", "is_active", "user"])
        audit.record(
            module=AuditModule.FLEET,
            action=AuditAction.DELETE,
            obj=driver,
            actor=actor,
            before=before,
            after={"deleted": True, "is_active": False, "user": None},
        )

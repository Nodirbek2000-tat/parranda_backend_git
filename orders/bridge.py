"""The only place where the orders app touches the logistics app (API_CONTRACT_V3 §6.2, §7.4, §7.5).

Orders never import logistics models or services at module level, so the two apps can be developed and
migrated independently and ``orders`` keeps working while ``logistics`` has no tables yet (approve with
``create_task=false`` still works; ``create_task`` answers ``409`` with :data:`MSG_LOGISTICS_UNAVAILABLE`).

Interface used in both directions (documented in parranda_backend/README.md, “Buyurtmalar”):

Orders → logistics (``logistics.services``):

``create_task_from_order(*, actor, order, items, data) -> LogisticsTask``
    Called inside the orders transaction with the ``Order`` row already locked (lock order §17.1:
    Order → LogisticsTask → Product …). ``items`` is a list of ``(OrderItem, Decimal quantity)`` with
    quantity > 0; ``data`` holds the optional ``planned_date``, ``vehicle``, ``driver``, ``responsible``
    (ids) and ``note`` of the request. It creates the task (``order_id`` = the order, ``household`` = order
    household, ``LogisticsTaskItem.order_item_id`` set), reserves stock (§7.4, raising
    ``ValidationError({"items": [...]})`` when stock is short), writes the task log + audit and fires
    ``task_created`` (+ ``driver_assigned``). It must NOT change the order status.

``cancel_task(*, actor, task_id | task, reason, sync_order=False) -> LogisticsTask``
    The §7.5 cancel (releases reservations). Called by :func:`orders.services.cancel_order`, which sets
    the order status itself (``sync_order=False``; parameters the function does not accept are not passed).

Logistics → orders (:mod:`logistics.hooks`, registered in :meth:`orders.apps.OrdersConfig.ready`):
``lock(order_id)`` → locked ``Order``; ``on_task_status(order, task, status, actor, note)`` → order sync;
``refs(ids)`` → ``{id: {"id", "code"}}``.
"""

from __future__ import annotations

import inspect
from collections.abc import Iterable
from typing import Any

from rest_framework import status
from rest_framework.exceptions import APIException

#: Task statuses as defined in API_CONTRACT_V3 §7.3 (the logistics app owns the model).
TASK_CANCELLED = "CANCELLED"
TASK_STARTED = frozenset({"IN_TRANSIT", "DELIVERED", "CONFIRMED"})
TASK_STATUS_DISPLAY = {
    "NEW": "Yangi",
    "ASSIGNED": "Biriktirildi",
    "ACCEPTED": "Ishga olindi",
    "IN_TRANSIT": "Yo‘lda",
    "DELIVERED": "Yetkazildi",
    "CONFIRMED": "Tasdiqlandi",
    "CANCELLED": "Bekor qilindi",
}

MSG_LOGISTICS_UNAVAILABLE = (
    "Logistika moduli hali ishga tushirilmagan. Buyurtmani topshiriqsiz tasdiqlang."
)


class LogisticsUnavailable(APIException):
    status_code = status.HTTP_409_CONFLICT
    default_detail = MSG_LOGISTICS_UNAVAILABLE
    default_code = "logistics_unavailable"


def task_model():
    """``logistics.LogisticsTask`` or ``None`` while the model (or its table) does not exist yet.

    The table check runs on the raw connection and is cached once positive (``warehouse.readiness``),
    so it never counts against the query budgets.
    """
    from warehouse.readiness import optional_model

    return optional_model("logistics", "LogisticsTask", "order_id")


def task_code(task) -> str:
    return getattr(task, "code", "") or f"LG-{task.pk:06d}"


def active_tasks(order_ids: Iterable[int]) -> dict[int, Any]:
    """``{order_id: task}`` of the non-cancelled task of each order (one query, or none)."""
    model = task_model()
    ids = list(order_ids)
    if model is None or not ids:
        return {}
    rows = model.objects.filter(order_id__in=ids).exclude(status=TASK_CANCELLED).order_by("id")
    return {task.order_id: task for task in rows}


def active_task(order_id: int, *, lock: bool = False):
    model = task_model()
    if model is None:
        return None
    qs = model.objects.filter(order_id=order_id).exclude(status=TASK_CANCELLED)
    if lock:
        qs = qs.select_for_update()
    return qs.order_by("id").first()


#: Not-finished task statuses (logistics ``ACTIVE_STATUSES``): reservations, a driver or a pending confirmation.
TASK_OPEN = ("NEW", "ASSIGNED", "ACCEPTED", "IN_TRANSIT", "DELIVERED")


def open_task_codes(order_id: int) -> list[str]:
    """Codes of the order's not-finished tasks (``[]`` while logistics has no tables)."""
    model = task_model()
    if model is None:
        return []
    rows = model.objects.filter(order_id=order_id, status__in=TASK_OPEN).order_by("id")
    return [task_code(task) for task in rows]


def task_ref(task) -> dict[str, Any] | None:
    """``{"id", "code", "status", "status_display"}`` of a task (Order object ``task``)."""
    if task is None:
        return None
    display = TASK_STATUS_DISPLAY.get(task.status, task.status)
    return {"id": task.pk, "code": task_code(task), "status": task.status, "status_display": display}


def available() -> bool:
    """Task creation is possible (model + table + ``create_task_from_order`` exist)."""
    if task_model() is None:
        return False
    try:
        from logistics import services as logistics_services
    except ImportError:  # pragma: no cover - the app is always installed
        return False
    return callable(getattr(logistics_services, "create_task_from_order", None))


def _service(name: str):
    try:
        from logistics import services as logistics_services
    except ImportError:  # pragma: no cover - the app is always installed
        raise LogisticsUnavailable() from None
    function = getattr(logistics_services, name, None)
    if function is None or task_model() is None:
        raise LogisticsUnavailable()
    return function


def _accepted(function, candidates: dict[str, Any]) -> dict[str, Any]:
    """Only the keyword arguments ``function`` accepts (all of them with ``**kwargs``)."""
    try:
        parameters = inspect.signature(function).parameters
    except (TypeError, ValueError):  # pragma: no cover - builtins
        return candidates
    if any(p.kind is inspect.Parameter.VAR_KEYWORD for p in parameters.values()):
        return candidates
    return {name: value for name, value in candidates.items() if name in parameters}


def create_task_from_order(*, actor, order, items, data: dict[str, Any]):
    return _service("create_task_from_order")(actor=actor, order=order, items=items, data=data)


def cancel_task(*, actor, task, reason: str):
    function = _service("cancel_task")
    kwargs = _accepted(
        function,
        {"actor": actor, "task_id": task.pk, "task": task, "reason": reason, "sync_order": False},
    )
    if "task_id" in kwargs and "task" in kwargs:
        kwargs.pop("task")
    return function(**kwargs)


def serialize_task(task, request) -> dict[str, Any]:
    """Full ``LogisticsTask`` object (§7.3) when the logistics serializer exists, else the short ref."""
    try:
        from logistics import serializers as logistics_serializers
    except ImportError:  # pragma: no cover
        return task_ref(task) or {}
    serializer = getattr(logistics_serializers, "LogisticsTaskSerializer", None)
    if serializer is None:
        return task_ref(task) or {}
    return serializer(task, context={"request": request}).data

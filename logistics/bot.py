"""``GET /api/bot/users/{tid}/tasks`` payloads (API_CONTRACT_V3 §14.2).

LOGISTICS, WAREHOUSE, OWNER and ADMIN are built here. Other modules add their parts through
:func:`register_extension` from their ``AppConfig.ready()`` (the vet team: the whole VET payload, ``upcoming_vet``
for OWNER and ``vet_overdue`` for ADMIN)::

    from logistics import bot
    bot.register_extension(Role.VET, lambda user, payload: payload.update(visits_today=[...], ...))
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from django.apps import apps
from django.db.models import F
from django.utils import timezone

from accounts.models import User
from common.roles import Role, role_of
from notifications.compose import items_text
from warehouse.models import Product

from . import selectors
from .models import TaskStatus
from .serializers import LogisticsTaskShortSerializer

Extension = Callable[[User, dict[str, Any]], None]
_EXTENSIONS: dict[str, list[Extension]] = {}
LIMIT = selectors.BOT_LIMIT


def register_extension(role: str, fn: Extension) -> None:
    _EXTENSIONS.setdefault(role, []).append(fn)


def _short(tasks, user: User) -> list[dict]:
    return LogisticsTaskShortSerializer(list(tasks), many=True, context={"user": user}).data


def _order_model():
    try:
        return apps.get_model("orders", "Order")
    except LookupError:  # pragma: no cover - the app is always installed
        return None


def _logistics(user: User) -> dict[str, Any]:
    return {"tasks": _short(selectors.bot_tasks_for_logistics(user), user)}


def _warehouse(user: User) -> dict[str, Any]:
    from warehouse import lots

    tasks = selectors.task_queryset(user).filter(status__in=(TaskStatus.ASSIGNED, TaskStatus.ACCEPTED))
    today = timezone.localdate()
    return {
        "to_load": _short(selectors._bot_order(tasks, today), user),
        "low_stock_count": Product.objects.filter(is_active=True, balance__lte=F("min_stock")).count(),
        "expiring_lots": lots.expiring_lots(today, lots.expiry_soon_days()).count(),
    }


def _owner(user: User) -> dict[str, Any]:
    tasks = selectors.task_queryset(user)
    payload: dict[str, Any] = {
        "deliveries_to_confirm": _short(tasks.filter(status=TaskStatus.DELIVERED).order_by("-id")[:LIMIT], user),
        "in_transit": _short(tasks.filter(status=TaskStatus.IN_TRANSIT).order_by("-id")[:LIMIT], user),
        "open_orders": [],
        "upcoming_vet": [],
    }
    order_model = _order_model()
    if order_model is not None:
        rows = (
            order_model.objects.filter(household__owner=user)
            .exclude(status__in=("REJECTED", "CONFIRMED", "CANCELLED"))
            .order_by("-id")[:LIMIT]
        )
        payload["open_orders"] = [
            {"id": order.pk, "code": order.code, "status": order.status, "status_display": order.get_status_display()}
            for order in rows
        ]
    return payload


def _admin(user: User) -> dict[str, Any]:
    summary = selectors.summary(user)
    payload: dict[str, Any] = {
        "orders_new": [],
        "orders_new_count": 0,
        "deliveries": {
            "in_transit": summary["by_status"].get(TaskStatus.IN_TRANSIT, 0),
            "delivered": summary["by_status"].get(TaskStatus.DELIVERED, 0),
            "overdue": summary["overdue"],
        },
        "vet_overdue": 0,
    }
    order_model = _order_model()
    if order_model is not None:
        new = order_model.objects.filter(status="NEW")
        payload["orders_new_count"] = new.count()
        rows = new.select_related("household").prefetch_related("items__product").order_by("created_at", "id")[:LIMIT]
        payload["orders_new"] = [
            {
                "id": order.pk,
                "code": order.code,
                "household": {"id": order.household.pk, "code": order.household.code, "name": order.household.name},
                "items_text": items_text(
                    [(item.product.name, item.requested_qty, item.product.unit) for item in order.items.all()]
                ),
                "created_at": timezone.localtime(order.created_at).isoformat(),
            }
            for order in rows
        ]
    return payload


_BUILDERS: dict[str, Callable[[User], dict[str, Any]]] = {
    Role.LOGISTICS: _logistics,
    Role.WAREHOUSE: _warehouse,
    Role.OWNER: _owner,
    Role.ADMIN: _admin,
}


def tasks_payload(user: User) -> dict[str, Any]:
    role = role_of(user) or ""
    payload: dict[str, Any] = {"role": role}
    builder = _BUILDERS.get(role)
    if builder is not None:
        payload.update(builder(user))
    for extension in _EXTENSIONS.get(role, []):
        extension(user, payload)
    return payload

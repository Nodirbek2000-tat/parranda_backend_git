"""Read-side helpers of the orders app: scoping, allowed actions, dashboard / bot / search data.

Other modules (dashboard, bot summary, ``/tasks``, global search) call these functions instead of querying
the orders tables themselves, so the business rules live in one place. Every function is read-only and runs
a fixed number of queries.
"""

from __future__ import annotations

from typing import Any

from django.db.models import Count, Exists, OuterRef, Q, QuerySet
from rest_framework.fields import DateTimeField

from accounts.models import User
from common.roles import Role, role_of
from notifications.compose import items_text

from . import bridge
from .models import FINAL_STATUSES, OPEN_STATUSES, Order, OrderStatus

#: Max rows of the dashboard ``pending_orders`` card (§10.1) and of the bot ``orders_new`` list (§14.1).
PENDING_LIMIT = 5
BOT_NEW_LIMIT = 10

_DATETIME = DateTimeField()  # ISO 8601 in Asia/Tashkent, like every serializer


def tables_ready() -> bool:
    """The orders tables exist (`False` on the live database before the Fix-phase `migrate`)."""
    from warehouse.readiness import models_ready

    return models_ready(Order)


def scoped_orders(user: User) -> QuerySet[Order]:
    """Orders the user may read: ADMIN / WAREHOUSE all, OWNER own household only, others none."""
    qs = Order.objects.all()
    role = role_of(user)
    if role == Role.OWNER:
        return qs.filter(household__owner=user)
    if role in (Role.ADMIN, Role.WAREHOUSE):
        return qs
    return qs.none()


def is_own(order: Order, user: User | None) -> bool:
    return user is not None and order.household.owner_id == getattr(user, "pk", None)


def allowed_actions(order: Order, user: User | None, task=None, logistics_ready: bool | None = None) -> list[str]:
    """``allowed_actions`` of the Order object (§6.3) for the requesting user (UI shows only these)."""
    role = role_of(user) if user is not None else None
    status = order.status
    actions: list[str] = []
    if role == Role.ADMIN:
        if status == OrderStatus.NEW:
            actions += ["approve", "reject", "cancel"]
        elif status == OrderStatus.APPROVED:
            if task is None or task.status not in bridge.TASK_STARTED:
                actions.append("cancel")
            if task is None:
                if logistics_ready is None:
                    logistics_ready = bridge.available()
                if logistics_ready:
                    actions.append("create_task")
        elif status == OrderStatus.DELIVERED and task is not None:
            actions.append("confirm_receipt")
    elif role == Role.OWNER and is_own(order, user):
        if status == OrderStatus.NEW:
            actions.append("cancel")
        elif status == OrderStatus.DELIVERED and task is not None:
            actions.append("confirm_receipt")
    return actions


# --- Dashboards (§10.1, §10.2) ---------------------------------------------------------------------


def _with_task_condition() -> Q:
    """``Q`` true for orders that have a non-cancelled logistics task (empty ``Q`` if logistics is not ready)."""
    model = bridge.task_model()
    if model is None:
        return Q(pk__in=[])
    return Q(Exists(model.objects.filter(order_id=OuterRef("pk")).exclude(status=bridge.TASK_CANCELLED)))


def admin_counts() -> dict[str, int]:
    """``{"new", "approved_without_task", "in_delivery", "awaiting_confirmation"}`` (one query)."""
    approved = Q(status=OrderStatus.APPROVED)
    totals = Order.objects.aggregate(
        new=Count("id", filter=Q(status=OrderStatus.NEW)),
        approved_without_task=Count("id", filter=approved & ~_with_task_condition()),
        in_delivery=Count("id", filter=Q(status=OrderStatus.IN_DELIVERY)),
        awaiting_confirmation=Count("id", filter=Q(status=OrderStatus.DELIVERED)),
    )
    return {key: int(value or 0) for key, value in totals.items()}


def pending_orders(limit: int = PENDING_LIMIT) -> list[dict[str, Any]]:
    """NEW orders, oldest first (dashboard card ``pending_orders``, one query)."""
    rows = (
        Order.objects.filter(status=OrderStatus.NEW)
        .select_related("household")
        .annotate(items_count=Count("items"))
        .order_by("created_at", "id")[:limit]
    )
    return [
        {
            "id": order.pk,
            "code": order.code,
            "household": {"id": order.household_id, "code": order.household.code, "name": order.household.name},
            "kind_display": order.get_kind_display(),
            "items_count": order.items_count,
            "created_at": _DATETIME.to_representation(order.created_at),
        }
        for order in rows
    ]


def admin_dashboard_block() -> dict[str, Any]:
    """The orders keys of ``GET /api/dashboard/admin`` (§10.1): ``orders`` + ``pending_orders`` (2 queries).
    Zeros while the orders tables are not migrated yet (the Phase 1 dashboard keeps working)."""
    if not tables_ready():
        empty = {"new": 0, "approved_without_task": 0, "in_delivery": 0, "awaiting_confirmation": 0}
        return {"orders": empty, "pending_orders": []}
    return {"orders": admin_counts(), "pending_orders": pending_orders()}


def owner_counts(household) -> dict[str, int]:
    """``GET /api/dashboard/owner`` ``orders`` (§10.2): ``open`` = not final (incl. delivered),
    ``awaiting_confirmation`` = delivered, waiting for the owner's confirmation (one query)."""
    if not tables_ready():
        return {"open": 0, "awaiting_confirmation": 0}
    totals = Order.objects.filter(household=household).aggregate(
        open=Count("id", filter=Q(status__in=OPEN_STATUSES)),
        awaiting_confirmation=Count("id", filter=Q(status=OrderStatus.DELIVERED)),
    )
    return {key: int(value or 0) for key, value in totals.items()}


# --- Bot (§14.1 ``/tasks``) -----------------------------------------------------------------------


def bot_new_orders(limit: int = BOT_NEW_LIMIT) -> tuple[list[dict[str, Any]], int]:
    """ADMIN ``orders_new`` (≤ ``limit``, oldest first) and ``orders_new_count`` (2 queries)."""
    qs = Order.objects.filter(status=OrderStatus.NEW)
    rows = list(qs.select_related("household").prefetch_related("items__product").order_by("created_at", "id")[:limit])
    items = [
        {
            "id": order.pk,
            "code": order.code,
            "household": {"id": order.household_id, "code": order.household.code, "name": order.household.name},
            "items_text": items_text(
                [(item.product.name, item.requested_qty, item.product.unit) for item in order.items.all()]
            ),
            "created_at": _DATETIME.to_representation(order.created_at),
        }
        for order in rows
    ]
    count = len(items) if len(items) < limit else qs.count()
    return items, count


def owner_open_orders(household, limit: int = 10) -> list[dict[str, Any]]:
    """OWNER ``open_orders``: ``[{id, code, status_display}]`` of not-final orders, newest first."""
    rows = (
        Order.objects.filter(household=household)
        .exclude(status__in=FINAL_STATUSES)
        .only("id", "status")
        .order_by("-created_at", "-id")[:limit]
    )
    return [{"id": o.pk, "code": o.code, "status": o.status, "status_display": o.get_status_display()} for o in rows]


# --- Global search (§12) ------------------------------------------------------------------------------


def search_group(user: User, q: str, limit: int) -> list[dict[str, Any]]:
    """``order`` group: code (exact ``BY-000005`` or contains), household name / code; one query."""
    from core import search

    if not tables_ready():  # live database before the Fix-phase migrate: the group stays empty
        return []
    condition = Q(household__name__icontains=q)
    pk = search.code_pk(q, "BY")
    if pk is not None:
        condition |= Q(pk=pk)
    household_pk = search.code_pk(q, "XN")
    if household_pk is not None:
        condition |= Q(household_id=household_pk)
    owner = role_of(user) == Role.OWNER
    rows = scoped_orders(user).filter(condition).select_related("household").order_by("-created_at", "-id")[:limit]
    return [
        search.item(
            order.pk,
            order.code,
            f"{order.household.name} · {order.get_status_display()}",
            f"/my/orders/{order.pk}" if owner else f"/orders/{order.pk}",
        )
        for order in rows
    ]

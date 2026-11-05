"""Read side of the logistics app: list scopes and filters, summary (§7.6), search group, bot task lists."""

from __future__ import annotations

from datetime import datetime, time, timedelta
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from django.db.models import Count, F, Prefetch, Q, QuerySet
from django.utils import timezone
from rest_framework.exceptions import ValidationError
from rest_framework.request import Request

from accounts.models import User
from common import query
from common.roles import Role, role_of
from core import search

from .models import (
    ACTIVE_STATUSES,
    OVERDUE_STATUSES,
    WORKING_STATUSES,
    Driver,
    LogisticsTask,
    LogisticsTaskItem,
    TaskStatus,
    Vehicle,
)
from .services import visible_tasks

SUMMARY_DAYS = 30
BOT_LIMIT = 10


def overdue_q(today) -> Q:
    return Q(planned_date__lt=today, status__in=OVERDUE_STATUSES)


def mine_q(user: User) -> Q:
    return Q(responsible_id=user.pk) | Q(driver__user_id=user.pk)


def task_queryset(user: User) -> QuerySet[LogisticsTask]:
    items = LogisticsTaskItem.objects.select_related("product", "stock_transaction").order_by("id")
    return (
        visible_tasks(user)
        .select_related("household__owner", "vehicle", "driver__user", "responsible", "created_by")
        .prefetch_related(Prefetch("items", queryset=items))
    )


def ordered(qs: QuerySet[LogisticsTask]) -> QuerySet[LogisticsTask]:
    """``planned_date`` ascending NULLS LAST, then ``id`` descending (§7.5)."""
    return qs.order_by(F("planned_date").asc(nulls_last=True), "-id")


def _id_or_me(request: Request, name: str) -> int | None:
    raw = query.get_str(request, name)
    if raw.lower() == "me":
        return request.user.pk
    return query.get_int(request, name)


def filter_tasks(qs: QuerySet[LogisticsTask], request: Request) -> QuerySet[LogisticsTask]:
    raw_status = query.get_str(request, "status")
    if raw_status:
        statuses = [part.strip() for part in raw_status.split(",") if part.strip()]
        bad = [value for value in statuses if value not in TaskStatus.values]
        if bad:
            raise ValidationError({"status": [f"“{bad[0]}” ruxsat etilgan qiymat emas."]})
        qs = qs.filter(status__in=statuses)
    for name in ("household", "driver", "vehicle"):
        value = query.get_int(request, name)
        if value is not None:
            qs = qs.filter(**{f"{name}_id": value})
    responsible = _id_or_me(request, "responsible")
    if responsible is not None:
        qs = qs.filter(responsible_id=responsible)
    if query.get_bool(request, "mine"):
        qs = qs.filter(mine_q(request.user))
    if query.get_bool(request, "overdue"):
        qs = qs.filter(overdue_q(timezone.localdate()))
    qs = query.apply_date_range(qs, request, "planned_date")
    term = query.get_str(request, "search")
    if term:
        condition = (
            Q(code__icontains=term)
            | Q(household__name__icontains=term)
            | Q(address_text__icontains=term)
            | Q(contact_name__icontains=term)
            | Q(driver__full_name__icontains=term)
            | Q(vehicle__plate_number__icontains=term)
        )
        pk = search.code_pk(term, "LG")
        if pk is not None:
            condition |= Q(pk=pk)
        household_pk = search.code_pk(term, "XN")
        if household_pk is not None:
            condition |= Q(household_id=household_pk)
        order_pk = search.code_pk(term, "BY")
        if order_pk is not None:
            condition |= Q(order_id=order_pk)
        qs = qs.filter(condition)
    return qs


def vehicles_queryset() -> QuerySet[Vehicle]:
    return Vehicle.objects.annotate(
        active_tasks=Count("tasks", filter=Q(tasks__status__in=WORKING_STATUSES), distinct=True)
    ).order_by("plate_number", "id")


def drivers_queryset() -> QuerySet[Driver]:
    return (
        Driver.objects.select_related("user")
        .annotate(active_tasks=Count("tasks", filter=Q(tasks__status__in=WORKING_STATUSES), distinct=True))
        .order_by("full_name", "id")
    )


# --- Summary (§7.6) ----------------------------------------------------------------------------


def _local_day_start(day) -> datetime:
    return timezone.make_aware(datetime.combine(day, time.min))


def is_on_time(planned_date, delivered_at) -> bool | None:
    """THE punctuality rule (summary, reports): delivered on or before the planned day (local date).

    ``None`` when the task has no plan date or is not delivered — it is not judged.
    """
    if planned_date is None or delivered_at is None:
        return None
    return timezone.localtime(delivered_at).date() <= planned_date


def delivery_kpis(rows) -> dict[str, Any]:
    """``rows`` = ``(planned_date, delivered_at, created_at)`` of tasks DELIVERED in the window.

    Returns ``delivered``, ``judged`` (with a plan date), ``on_time`` and the unrounded ``lead_hours`` list —
    shared by ``/api/logistics/summary`` and the ``logistics_efficiency`` report so both show the same numbers.
    """
    delivered = judged = on_time = 0
    lead_hours: list[float] = []
    for planned_date, delivered_at, created_at in rows:
        delivered += 1
        lead_hours.append((delivered_at - created_at).total_seconds() / 3600)
        punctual = is_on_time(planned_date, delivered_at)
        if punctual is not None:
            judged += 1
            on_time += 1 if punctual else 0
    return {"delivered": delivered, "judged": judged, "on_time": on_time, "lead_hours": lead_hours}


def summary(user: User) -> dict[str, Any]:
    """≤ 3 queries, independent of data size."""
    today = timezone.localdate()
    day_start = _local_day_start(today)
    since = timezone.now() - timedelta(days=SUMMARY_DAYS)
    tasks = LogisticsTask.objects.all()

    by_status = {status: 0 for status in ACTIVE_STATUSES}
    for row in tasks.filter(status__in=ACTIVE_STATUSES).values("status").annotate(n=Count("id")).order_by():
        by_status[row["status"]] = row["n"]

    mine = mine_q(user)
    totals = tasks.aggregate(
        mine_active=Count("id", filter=mine & Q(status__in=WORKING_STATUSES)),
        mine_in_transit=Count("id", filter=mine & Q(status=TaskStatus.IN_TRANSIT)),
        overdue=Count("id", filter=overdue_q(today)),
        delivered_today=Count("id", filter=Q(delivered_at__gte=day_start)),
        confirmed_today=Count("id", filter=Q(confirmed_at__gte=day_start)),
        awaiting_confirmation=Count("id", filter=Q(status=TaskStatus.DELIVERED)),
        new_unassigned=Count("id", filter=Q(status=TaskStatus.NEW)),
        created=Count("id", filter=Q(created_at__gte=since)),
        delivered=Count("id", filter=Q(delivered_at__gte=since)),
        confirmed=Count("id", filter=Q(confirmed_at__gte=since)),
        cancelled=Count("id", filter=Q(cancelled_at__gte=since)),
        with_issue=Count("id", filter=Q(confirmed_at__gte=since, received_ok=False)),
    )

    kpis = delivery_kpis(tasks.filter(delivered_at__gte=since).values_list("planned_date", "delivered_at", "created_at"))
    on_time, with_plan, lead_hours = kpis["on_time"], kpis["judged"], kpis["lead_hours"]

    def one_dp(value: float) -> float:
        return float(Decimal(str(value)).quantize(Decimal("0.1"), rounding=ROUND_HALF_UP))

    return {
        "date": today.isoformat(),
        "by_status": by_status,
        "mine": {"active": totals["mine_active"], "in_transit": totals["mine_in_transit"]},
        "overdue": totals["overdue"],
        "delivered_today": totals["delivered_today"],
        "confirmed_today": totals["confirmed_today"],
        "awaiting_confirmation": totals["awaiting_confirmation"],
        "new_unassigned": totals["new_unassigned"],
        "last_30d": {
            "created": totals["created"],
            "delivered": totals["delivered"],
            "confirmed": totals["confirmed"],
            "cancelled": totals["cancelled"],
            "with_issue": totals["with_issue"],
            "on_time_pct": one_dp(100 * on_time / with_plan) if with_plan else None,
            "avg_lead_time_h": one_dp(sum(lead_hours) / len(lead_hours)) if lead_hours else None,
        },
    }


def dashboard_counts() -> dict[str, int]:
    """The ``logistics`` block of ``GET /api/dashboard/admin`` (§10.1) — one query, for the dashboard module."""
    today = timezone.localdate()
    totals = LogisticsTask.objects.aggregate(
        **{status.lower(): Count("id", filter=Q(status=status)) for status in ACTIVE_STATUSES},
        overdue=Count("id", filter=overdue_q(today)),
        delivered_today=Count("id", filter=Q(delivered_at__gte=_local_day_start(today))),
    )
    return {key: value or 0 for key, value in totals.items()}


DASHBOARD_ZEROS = {
    "new": 0, "assigned": 0, "accepted": 0, "in_transit": 0, "delivered": 0, "overdue": 0, "delivered_today": 0,
}


def tables_ready() -> bool:
    """The logistics tables exist (``False`` on the live database before the Fix-phase ``migrate``)."""
    from warehouse.readiness import models_ready

    return models_ready(LogisticsTask)


def admin_dashboard_block() -> dict[str, Any]:
    """``{"logistics": {...}}`` for ``GET /api/dashboard/admin`` (§10.1); zeros until the tables are migrated."""
    return {"logistics": dashboard_counts() if tables_ready() else dict(DASHBOARD_ZEROS)}


def owner_dashboard_block(user: User) -> dict[str, Any]:
    """``deliveries_to_confirm`` of ``GET /api/dashboard/owner`` (§10.2): own DELIVERED tasks, newest first."""
    if not tables_ready():
        return {"deliveries_to_confirm": []}
    from .serializers import LogisticsTaskShortSerializer

    tasks = list(task_queryset(user).filter(status=TaskStatus.DELIVERED).order_by("-id")[:BOT_LIMIT])
    return {"deliveries_to_confirm": LogisticsTaskShortSerializer(tasks, many=True, context={"user": user}).data}


# --- Global search group ------------------------------------------------------------------------


def owner_task_path(task: LogisticsTask) -> str:
    return f"/my/orders/{task.order_id}" if task.order_id else "/my/deliveries"


def search_tasks(user: User, q: str, limit: int) -> list[dict]:
    qs = visible_tasks(user).select_related("household")
    condition = Q(code__icontains=q) | Q(household__name__icontains=q) | Q(address_text__icontains=q)
    pk = search.code_pk(q, "LG")
    if pk is not None:
        condition |= Q(pk=pk)
    is_owner = role_of(user) == Role.OWNER
    return [
        search.item(
            task.pk,
            task.code,
            f"{task.household.name} · {task.get_status_display()}",
            owner_task_path(task) if is_owner else f"/logistics/{task.pk}",
        )
        for task in qs.filter(condition).order_by("-id")[:limit]
    ]


# --- Bot task lists (§14.2) --------------------------------------------------------------------


def _bot_order(qs: QuerySet[LogisticsTask], today) -> list[LogisticsTask]:
    """Overdue first, then ``planned_date`` (NULLS LAST), then newest."""
    tasks = list(qs.order_by(F("planned_date").asc(nulls_last=True), "-id")[: BOT_LIMIT * 3])
    tasks.sort(key=lambda task: not (task.planned_date is not None and task.planned_date < today))
    return tasks[:BOT_LIMIT]


def bot_tasks_for_logistics(user: User) -> list[LogisticsTask]:
    today = timezone.localdate()
    qs = task_queryset(user).filter(
        (mine_q(user) & Q(status__in=WORKING_STATUSES)) | Q(status=TaskStatus.NEW)
    )
    return _bot_order(qs, today)


def bot_tasks_to_load() -> QuerySet[LogisticsTask]:
    return LogisticsTask.objects.filter(status__in=(TaskStatus.ASSIGNED, TaskStatus.ACCEPTED))


def bot_today(user: User) -> dict[str, int]:
    """``today`` of the LOGISTICS bot summary (§10.3, ``/reports``)."""
    data = summary(user)
    return {
        "my_active": data["mine"]["active"],
        "my_in_transit": data["mine"]["in_transit"],
        "new_unassigned": data["new_unassigned"],
        "overdue": data["overdue"],
        "delivered_today": data["delivered_today"],
        "awaiting_confirmation": data["awaiting_confirmation"],
    }

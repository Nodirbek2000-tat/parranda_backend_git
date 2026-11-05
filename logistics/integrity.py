"""Logistics invariants for ``manage.py check_integrity`` (API_CONTRACT_V3 §16.3). Read-only."""

from __future__ import annotations

from collections import defaultdict
from decimal import Decimal

from django.db.models import Sum

from warehouse.models import Product

from .models import STARTED_STATUSES, LogisticsTask, LogisticsTaskItem, TaskStatus

ZERO = Decimal("0")
RESERVING = (TaskStatus.NEW, TaskStatus.ASSIGNED, TaskStatus.ACCEPTED)

ORDER_STATUS_FOR_TASK = {
    TaskStatus.IN_TRANSIT: "IN_DELIVERY",
    TaskStatus.DELIVERED: "DELIVERED",
    TaskStatus.CONFIRMED: "CONFIRMED",
}


def problems() -> list[str]:
    found: list[str] = []
    reserved: dict[int, Decimal] = defaultdict(lambda: ZERO)
    rows = (
        LogisticsTaskItem.objects.filter(task__status__in=RESERVING)
        .order_by()
        .values("product_id")
        .annotate(total=Sum("reserved_qty"))
    )
    for row in rows:
        reserved[row["product_id"]] = row["total"] or ZERO
    for product in Product.all_objects.only("id", "name", "reserved").order_by("id"):
        expected = reserved.get(product.pk, ZERO)
        if product.reserved != expected:
            found.append(
                f"“{product.name}”: zaxiralangan {product.reserved}, topshiriqlar bo‘yicha {expected} bo‘lishi kerak"
            )
    for item in LogisticsTaskItem.objects.filter(task__status__in=STARTED_STATUSES, stock_transaction__isnull=True):
        found.append(f"Topshiriq #{item.task_id}: “{item.product_id}” mahsuloti uchun chiqim operatsiyasi yo‘q")
    for item in LogisticsTaskItem.objects.filter(task__status__in=RESERVING, stock_transaction__isnull=False):
        found.append(f"Topshiriq #{item.task_id}: boshlanmagan topshiriqda chiqim operatsiyasi bor")
    # ``all_objects``: soft-deleted tasks keep their stock operations and order link and must still add up.
    tasks = LogisticsTask.all_objects.filter(order__isnull=False, status__in=list(ORDER_STATUS_FOR_TASK)).select_related(
        "order"
    )
    for task in tasks:
        expected_status = ORDER_STATUS_FOR_TASK[task.status]
        if task.order.status != expected_status:
            found.append(
                f"{task.code}: buyurtma {task.order.code} holati {task.order.status}, {expected_status} bo‘lishi kerak"
            )
    return found

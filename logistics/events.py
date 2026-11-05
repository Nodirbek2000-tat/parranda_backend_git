"""Notification texts and recipients of the logistics app (API_CONTRACT_V3 §9.4).

Called inside the business transaction; rows are created after commit by ``notify_on_commit`` (idempotent
``event_key``). Body rows: ``Topshiriq``, ``Xonadon``, ``Mas’ul``, ``Manzil``, ``Mahsulotlar``, ``Transport``,
``Haydovchi``, ``Reja sanasi``, ``Izoh``; op id = task code. Staff path ``/logistics/{id}``, owner path
``/my/orders/{order_id}`` (task of an order) or ``/my/deliveries``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from common.formatting import fmt_date
from notifications import registry
from notifications.compose import Row, compose, household_rows, items_text
from notifications.services import notify_on_commit

if TYPE_CHECKING:
    from .models import LogisticsTask

STAFFED_OR_LATER = {"ASSIGNED", "ACCEPTED", "IN_TRANSIT", "DELIVERED", "CONFIRMED"}


def owner_path(task: LogisticsTask) -> str:
    return f"/my/orders/{task.order_id}" if task.order_id else "/my/deliveries"


def vehicle_text(task: LogisticsTask) -> str:
    vehicle = task.vehicle
    if vehicle is None:
        return ""
    return f"{vehicle.plate_number}, {vehicle.model}" if vehicle.model else vehicle.plate_number


def driver_text(task: LogisticsTask) -> str:
    driver = task.driver
    if driver is None:
        return ""
    return f"{driver.full_name}, {driver.phone}" if driver.phone else driver.full_name


def task_items_text(task: LogisticsTask) -> str:
    items = [(item.product.name, item.quantity, item.product.unit) for item in task.items.select_related("product")]
    return items_text(items)


def _rows(task: LogisticsTask, *, extra: list[Row] | None = None, note: str | None = None) -> list[Row]:
    rows: list[Row] = [
        ("Topshiriq", task.code),
        *household_rows(task.household),
        ("Manzil", task.address_text),
        ("Mahsulotlar", task_items_text(task)),
        ("Transport", vehicle_text(task)),
        ("Haydovchi", driver_text(task)),
        ("Reja sanasi", fmt_date(task.planned_date) if task.planned_date else ""),
    ]
    rows.extend(extra or [])
    rows.append(("Izoh", task.note if note is None else note))
    return rows


def _notify(event: str, task: LogisticsTask, *, key, status: str, rows: list[Row], **kwargs) -> None:
    notify_on_commit(
        event,
        key=key,
        text=compose(event, rows, status, task.code),
        staff_path=f"/logistics/{task.pk}",
        owner_path=owner_path(task),
        related_op=task.code,
        object_type="LogisticsTask",
        object_id=task.pk,
        **kwargs,
    )


def _driver_user(task: LogisticsTask):
    driver = task.driver
    return driver.user if driver is not None and driver.user_id else None


def task_created(task: LogisticsTask) -> None:
    extra = [("Buyurtma", f"BY-{task.order_id:06d}")] if task.order_id else []
    _notify("task_created", task, key=task.pk, status=task.get_status_display(), rows=_rows(task, extra=extra))


def driver_assigned(task: LogisticsTask) -> None:
    """Key ``{task_id}.{assign_seq}``: every (re)assignment notifies once."""
    _notify(
        "driver_assigned",
        task,
        key=f"{task.pk}.{task.assign_seq}",
        status=task.get_status_display(),
        rows=_rows(task),
        contextual={registry.DRIVER_USER: _driver_user(task), registry.RESPONSIBLE: task.responsible},
    )


def delivery_started(task: LogisticsTask) -> None:
    _notify(
        "delivery_started",
        task,
        key=task.pk,
        status=task.get_status_display(),
        rows=_rows(task, extra=[("Yo‘lga chiqdi", fmt_date(task.started_at))]),
        contextual={registry.HOUSEHOLD_OWNER: task.household.owner},
    )


def delivery_delivered(task: LogisticsTask) -> None:
    extra: list[Row] = [("Qabul qildi", task.recipient_name), ("Yetkazildi", fmt_date(task.delivered_at))]
    _notify(
        "delivery_delivered",
        task,
        key=task.pk,
        status=task.get_status_display(),
        rows=_rows(task, extra=extra, note=task.delivery_note),
        contextual={registry.HOUSEHOLD_OWNER: task.household.owner},
    )


def delivery_confirmed(task: LogisticsTask) -> None:
    problem = task.received_ok is False
    status = "Muammo bilan qabul qilindi" if problem else task.get_status_display()
    # ``issue_note`` is the problem text, or (received fine) the owner's comment / the admin's reason.
    extra: list[Row] = [
        ("Muammo" if problem else "Qabul izohi", task.issue_note),
        ("Tasdiqlandi", fmt_date(task.confirmed_at)),
    ]
    _notify(
        "delivery_confirmed",
        task,
        key=task.pk,
        status=status,
        rows=_rows(task, extra=extra),
        contextual={registry.RESPONSIBLE: task.responsible},
    )


def task_cancelled(task: LogisticsTask, *, old_status: str) -> None:
    """Owner only when the task was already staffed (status ≥ ASSIGNED) or belongs to an order."""
    contextual = {registry.RESPONSIBLE: task.responsible, registry.DRIVER_USER: _driver_user(task)}
    if old_status in STAFFED_OR_LATER or task.order_id:
        contextual[registry.HOUSEHOLD_OWNER] = task.household.owner
    _notify(
        "task_cancelled",
        task,
        key=task.pk,
        status=task.get_status_display(),
        rows=_rows(task, extra=[("Sabab", task.cancel_reason)]),
        contextual=contextual,
    )

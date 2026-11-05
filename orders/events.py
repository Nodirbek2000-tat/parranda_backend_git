"""Notification texts and recipients of the orders app (API_CONTRACT_V3 §9.4).

Called inside the business transaction; rows are created after commit by ``notify_on_commit``.
Body rows: ``Buyurtma``, ``Turi``, ``Xonadon``, ``Mas’ul``, ``Mahsulotlar``, ``Izoh``/``Sabab``, ``Sana``;
op id = order code. Role recipients come from the registry (and the Super Admin settings).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from django.utils import timezone

from common.formatting import fmt_date
from common.roles import Role
from notifications import registry
from notifications.compose import Row, compose, household_rows, items_text
from notifications.services import notify_on_commit, role_users

if TYPE_CHECKING:
    from .models import Order, OrderItem


def _paths(order: Order) -> dict[str, str]:
    return {"staff_path": f"/orders/{order.pk}", "owner_path": f"/my/orders/{order.pk}"}


def _rows(order: Order, items: list[tuple[str, object, str]], *, note_label: str, note: str, when) -> list[Row]:
    return [
        ("Buyurtma", order.code),
        ("Turi", order.get_kind_display()),
        *household_rows(order.household),
        ("Mahsulotlar", items_text(items)),
        (note_label, note),
        ("Sana", fmt_date(when)),
    ]


def _requested(order: Order) -> list[tuple[str, object, str]]:
    return [
        (item.product.name, item.requested_qty, item.product.unit)
        for item in order.items.select_related("product").order_by("id")
    ]


def order_created(order: Order) -> None:
    rows = _rows(order, _requested(order), note_label="Izoh", note=order.note, when=order.created_at)
    if order.desired_date:
        rows.insert(-1, ("Kerakli sana", fmt_date(order.desired_date)))
    notify_on_commit(
        "order_created",
        key=order.pk,
        text=compose("order_created", rows, order.get_status_display(), order.code),
        related_op=order.code,
        object_type="Order",
        object_id=order.pk,
        **_paths(order),
    )


def order_approved(order: Order, items: list[OrderItem], task=None) -> None:
    approved = [
        (item.product.name, item.approved_qty, item.product.unit) for item in items if item.approved_qty
    ]
    rows = _rows(order, approved, note_label="Izoh", note=order.decision_note, when=order.decided_at)
    if task is not None:
        code = getattr(task, "code", "") or f"LG-{task.pk:06d}"
        rows.insert(-1, ("Yetkazib berish", f"{code} topshirig‘i yaratildi"))
    notify_on_commit(
        "order_approved",
        key=order.pk,
        text=compose("order_approved", rows, order.get_status_display(), order.code),
        related_op=order.code,
        contextual={registry.HOUSEHOLD_OWNER: order.household.owner},
        object_type="Order",
        object_id=order.pk,
        **_paths(order),
    )


def order_rejected(order: Order) -> None:
    rows = _rows(order, _requested(order), note_label="Sabab", note=order.decision_note, when=order.decided_at)
    notify_on_commit(
        "order_rejected",
        key=order.pk,
        text=compose("order_rejected", rows, order.get_status_display(), order.code),
        related_op=order.code,
        contextual={registry.HOUSEHOLD_OWNER: order.household.owner},
        object_type="Order",
        object_id=order.pk,
        **_paths(order),
    )


def order_cancelled(order: Order, *, by_owner: bool) -> None:
    """``other_party``: ADMINs when the owner cancelled, the owner when staff cancelled."""
    other_party = list(role_users(Role.ADMIN)) if by_owner else [order.household.owner]
    rows = _rows(
        order,
        _requested(order),
        note_label="Sabab",
        note=order.cancel_reason,
        when=order.cancelled_at or timezone.now(),
    )
    notify_on_commit(
        "order_cancelled",
        key=order.pk,
        text=compose("order_cancelled", rows, order.get_status_display(), order.code),
        related_op=order.code,
        contextual={registry.OTHER_PARTY: other_party},
        object_type="Order",
        object_id=order.pk,
        **_paths(order),
    )

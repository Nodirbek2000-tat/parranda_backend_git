"""Bridge to the ``orders`` app (API_CONTRACT_V3 §6.2, §7.5 “order sync”).

The orders app calls :func:`logistics.services.create_task_from_order` / :func:`logistics.services.cancel_task`
through ``orders/bridge.py``; in the other direction every logistics transition of a task with ``order_id``
calls :func:`orders.services.sync_order_with_task` inside the logistics transaction (``IN_TRANSIT`` → order
``IN_DELIVERY``, ``DELIVERED`` → ``DELIVERED``, ``CONFIRMED`` → ``CONFIRMED``, ``CANCELLED`` → back to
``APPROVED``). The import is lazy so that both apps load (and migrate) independently.

Lock order (§17.1): the order row is locked **before** the task row.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any


def order_code(order_id: int) -> str:
    return f"BY-{order_id:06d}"


def lock_order(order_id: int | None) -> Any:
    if not order_id:
        return None
    from orders.services import lock_order as _lock

    return _lock(order_id)


def sync_order(task: Any, actor: Any, note: str = "") -> None:
    if not getattr(task, "order_id", None):
        return
    from orders.services import sync_order_with_task

    sync_order_with_task(task=task, actor=actor, note=note)


def order_ref(order_id: int | None) -> dict | None:
    return {"id": order_id, "code": order_code(order_id)} if order_id else None


def order_refs(order_ids: Iterable[int | None]) -> dict[int, dict]:
    return {pk: {"id": pk, "code": order_code(pk)} for pk in order_ids if pk}

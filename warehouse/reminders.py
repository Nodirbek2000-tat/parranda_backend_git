"""Daily lot reminders (API_CONTRACT_V3 §16.1 step 5): ``lot_expiring`` / ``lot_expired``.

Registered in :meth:`warehouse.apps.WarehouseConfig.ready`; the guard (once a day after ``reminder_hour``)
lives in :mod:`notifications.reminders`. Keys ``{lot_id}.soon`` / ``{lot_id}.expired`` make every re-run
idempotent (one message per lot and stage).
"""

from __future__ import annotations

from datetime import date, timedelta

from .events import lot_expiry
from .lots import expiry_soon_days
from .models import StockLot


def _open_lots():
    return StockLot.objects.select_related("product", "in_transaction").filter(
        remaining_qty__gt=0, expiry_date__isnull=False
    )


def lot_expiring_job(today: date) -> int:
    """Open lots with ``0 ≤ days_to_expiry ≤ expiry_reminder_days``."""
    soon = expiry_soon_days()
    queued = 0
    for lot in _open_lots().filter(expiry_date__gte=today, expiry_date__lte=today + timedelta(days=soon)).order_by(
        "expiry_date", "pk"
    ):
        queued += len(lot_expiry(lot, today, expired=False))
    return queued


def lot_expired_job(today: date) -> int:
    """Open lots with ``expiry_date < today``."""
    queued = 0
    for lot in _open_lots().filter(expiry_date__lt=today).order_by("expiry_date", "pk"):
        queued += len(lot_expiry(lot, today, expired=True))
    return queued

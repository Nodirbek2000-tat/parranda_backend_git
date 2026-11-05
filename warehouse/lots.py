"""Lot reads (API_CONTRACT_V3 §5.2): statuses, filters, ``nearest_expiry``, expiring lists.

Lot WRITES (IN creates a lot, FEFO consumption of OUT / ADJUST_OUT) live in :mod:`warehouse.services`
(``_post`` / ``_consume_lots``), under the product lock — this module never changes quantities.
"""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

from django.db.models import DateField, F, Min, OuterRef, QuerySet, Subquery, Sum
from django.db.models.functions import Coalesce

from .models import StockLot

ZERO = Decimal("0")
MAX_EXPIRING_WITHIN = 365
DEFAULT_SOON_DAYS = 30


class LotStatus:
    EMPTY = "EMPTY"
    EXPIRED = "EXPIRED"
    EXPIRING = "EXPIRING"
    OK = "OK"

    LABELS = {
        EMPTY: "Tugagan",
        EXPIRED: "Muddati o‘tgan",
        EXPIRING: "Muddati yaqin",
        OK: "Yaroqli",
    }
    VALUES = (EMPTY, EXPIRED, EXPIRING, OK)


def expiry_soon_days() -> int:
    """``SystemSettings.expiry_reminder_days`` (one query)."""
    from core.settings_service import get_settings

    return int(get_settings().expiry_reminder_days)


def days_to_expiry(lot: StockLot, today: date) -> int | None:
    return (lot.expiry_date - today).days if lot.expiry_date is not None else None


def lot_status(lot: StockLot, today: date, soon_days: int) -> str:
    """``EMPTY`` → ``EXPIRED`` → ``EXPIRING`` (``0 ≤ days ≤ soon_days``) → ``OK``."""
    if lot.remaining_qty <= 0:
        return LotStatus.EMPTY
    days = days_to_expiry(lot, today)
    if days is None:
        return LotStatus.OK
    if days < 0:
        return LotStatus.EXPIRED
    if days <= soon_days:
        return LotStatus.EXPIRING
    return LotStatus.OK


def lots_queryset() -> QuerySet[StockLot]:
    """Order of every lot list: ``expiry_date`` ascending NULLS LAST, then ``id``."""
    return StockLot.objects.select_related("product", "in_transaction").order_by(
        F("expiry_date").asc(nulls_last=True), "id"
    )


def filter_lots(
    qs: QuerySet[StockLot],
    *,
    today: date,
    product: int | None = None,
    expiring_within: int | None = None,
    expired: bool | None = None,
    has_remaining: bool | None = None,
) -> QuerySet[StockLot]:
    if product is not None:
        qs = qs.filter(product_id=product)
    if expiring_within is not None:
        # Open lots with ``expiry_date ≤ today + N`` (already expired ones included).
        qs = qs.filter(
            remaining_qty__gt=0, expiry_date__isnull=False, expiry_date__lte=today + timedelta(days=expiring_within)
        )
    if expired:
        qs = qs.filter(remaining_qty__gt=0, expiry_date__lt=today)
    if has_remaining:
        qs = qs.filter(remaining_qty__gt=0)
    return qs


def nearest_expiry_subquery() -> Subquery:
    """``min(expiry_date)`` of the product's lots with ``remaining_qty > 0`` (annotation, no N+1)."""
    return Subquery(
        StockLot.objects.filter(product_id=OuterRef("pk"), remaining_qty__gt=0, expiry_date__isnull=False)
        .order_by()
        .values("product_id")
        .annotate(first=Min("expiry_date"))
        .values("first")[:1],
        output_field=DateField(),
    )


def expiring_lots(today: date, soon_days: int) -> QuerySet[StockLot]:
    """Open lots that are expired or expire within ``soon_days`` (dashboard card, reminders, reports)."""
    return filter_lots(lots_queryset(), today=today, expiring_within=soon_days)


def tracked_by_product() -> dict[int, Decimal]:
    """``Σ remaining_qty`` per product (``untracked = balance − tracked``)."""
    rows = StockLot.objects.order_by().values("product_id").annotate(total=Coalesce(Sum("remaining_qty"), ZERO))
    return {row["product_id"]: row["total"] for row in rows}

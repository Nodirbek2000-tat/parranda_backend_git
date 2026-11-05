"""Bird counting rules (API contract, section 2). Every number is aggregated from movements.

- batch ``center_stock`` = initial − ΣDISTRIBUTE + ΣRETURN − ΣDEATH(no household) − ΣOUT(no household)
- household count for a batch = ΣDISTRIBUTE − ΣRETURN − ΣDEATH − ΣOUT (movements with that household)
- household ``bird_count`` = sum over batches
- batch ``distributed_total`` = ΣDISTRIBUTE − ΣRETURN, ``deaths_total`` = ΣDEATH (all)
"""

from __future__ import annotations

from datetime import date
from typing import TypedDict

from django.db.models import Case, F, IntegerField, Min, Q, QuerySet, Sum, Value, When
from django.db.models.functions import Coalesce

from .models import Household, MovementType, PoultryBatch, PoultryMovement, PoultryType

_ZERO = Value(0)
_INT = IntegerField()


def _household_delta(prefix: str = "") -> Case:
    """Signed effect of a movement on the household it belongs to."""
    quantity = F(f"{prefix}quantity")
    return Case(
        When(**{f"{prefix}type": MovementType.DISTRIBUTE}, then=quantity),
        default=-quantity,
        output_field=_INT,
    )


def _center_delta(prefix: str = "") -> Case:
    """Signed effect of a movement on the center stock of its batch."""
    quantity = F(f"{prefix}quantity")
    return Case(
        When(**{f"{prefix}type": MovementType.DISTRIBUTE}, then=-quantity),
        When(**{f"{prefix}type": MovementType.RETURN}, then=quantity),
        When(
            Q(**{f"{prefix}type__in": [MovementType.DEATH, MovementType.OUT]})
            & Q(**{f"{prefix}household__isnull": True}),
            then=-quantity,
        ),
        default=_ZERO,
        output_field=_INT,
    )


def _sum(expression, **filters) -> Coalesce:
    return Coalesce(Sum(expression, filter=Q(**filters) if filters else None), _ZERO, output_field=_INT)


# --- QuerySet annotations -------------------------------------------------------------


def batches_with_counts(qs: QuerySet[PoultryBatch] | None = None) -> QuerySet[PoultryBatch]:
    qs = PoultryBatch.objects.all() if qs is None else qs
    return qs.annotate(
        center_stock=F("initial_quantity") + _sum(_center_delta("movements__")),
        distributed_total=_sum("movements__quantity", movements__type=MovementType.DISTRIBUTE)
        - _sum("movements__quantity", movements__type=MovementType.RETURN),
        deaths_total=_sum("movements__quantity", movements__type=MovementType.DEATH),
    )


def households_with_counts(qs: QuerySet[Household] | None = None) -> QuerySet[Household]:
    qs = Household.objects.all() if qs is None else qs
    return qs.annotate(bird_count=_sum(_household_delta("movements__")))


# --- Point aggregates (used inside locked transactions) ------------------------------


def batch_center_stock(batch: PoultryBatch) -> int:
    delta = PoultryMovement.objects.filter(batch=batch).aggregate(v=_sum(_center_delta()))["v"]
    return batch.initial_quantity + delta


def household_batch_count(*, batch_id: int, household_id: int) -> int:
    return PoultryMovement.objects.filter(batch_id=batch_id, household_id=household_id).aggregate(
        v=_sum(_household_delta())
    )["v"]


class Availability(TypedDict):
    """Birds that a movement dated ``on_date`` may take away without any end-of-day count going negative.

    ``available = min_{d ≥ on_date} count(d)`` (end-of-day counts, API_CONTRACT_V2 §1.1), ``current`` = the
    count after the last movement, ``limit_day`` = the first day reaching the minimum when it is lower than
    ``current`` (the day that blocks a back-dated movement), else ``None``.
    """

    available: int
    current: int
    limit_day: date | None


def _availability(day_nets: list[tuple[date, int]], opening: int, on_date: date) -> Availability:
    """``day_nets`` = ``(date, net change)`` sorted by date; ``opening`` = count before the first of them."""
    count = opening
    lowest: int | None = None
    lowest_day: date | None = None
    for day, net in day_nets:
        if day > on_date and lowest is None:
            lowest, lowest_day = count, on_date  # count at the end of on_date (no movement on that day)
        count += net or 0
        if day >= on_date and (lowest is None or count < lowest):
            lowest, lowest_day = count, day
    if lowest is None:
        lowest, lowest_day = count, on_date
    return Availability(available=lowest, current=count, limit_day=lowest_day if lowest < count else None)


def household_batch_availability(*, batch_id: int, household_id: int, on_date: date) -> Availability:
    """Availability of one batch in one household for a DEATH / OUT / RETURN dated ``on_date`` (1 query)."""
    rows = (
        PoultryMovement.objects.filter(batch_id=batch_id, household_id=household_id)
        .order_by()
        .values("date")
        .annotate(net=_sum(_household_delta()))
        .order_by("date")
        .values_list("date", "net")
    )
    return _availability(list(rows), 0, on_date)


def household_availability(*, household_id: int, on_date: date) -> dict[int, Availability]:
    """``batch_id → Availability`` for every batch that ever had a movement in the household (1 query)."""
    rows = (
        PoultryMovement.objects.filter(household_id=household_id)
        .order_by()
        .values("batch_id", "date")
        .annotate(net=_sum(_household_delta()))
        .order_by("batch_id", "date")
        .values_list("batch_id", "date", "net")
    )
    per_batch: dict[int, list[tuple[date, int]]] = {}
    for batch_id, day, net in rows:
        per_batch.setdefault(batch_id, []).append((day, net))
    return {batch_id: _availability(nets, 0, on_date) for batch_id, nets in per_batch.items()}


def batch_center_availability(batch: PoultryBatch, on_date: date) -> Availability:
    """Center stock availability for a DISTRIBUTE / center DEATH / center OUT dated ``on_date ≥ arrived_at``."""
    rows = (
        PoultryMovement.objects.filter(batch=batch)
        .order_by()
        .values("date")
        .annotate(net=_sum(_center_delta()))
        .order_by("date")
        .values_list("date", "net")
    )
    return _availability(list(rows), batch.initial_quantity, on_date)


def household_bird_count(household_id: int) -> int:
    return PoultryMovement.objects.filter(household_id=household_id).aggregate(
        v=_sum(_household_delta())
    )["v"]


def birds_in_households() -> int:
    return PoultryMovement.objects.filter(household__isnull=False).aggregate(
        v=_sum(_household_delta())
    )["v"]


def birds_in_center() -> int:
    initial = PoultryBatch.objects.aggregate(v=_sum("initial_quantity"))["v"]
    delta = PoultryMovement.objects.aggregate(v=_sum(_center_delta()))["v"]
    return initial + delta


class HouseholdBatchRow(TypedDict):
    batch_id: int
    batch_code: str
    poultry_type: str
    poultry_type_display: str
    breed: str
    count: int
    #: TZ §5 "Parranda qabul qilingan sana": first DISTRIBUTE of this batch to the household.
    first_received_date: date | None


def household_batches(household_id: int) -> list[HouseholdBatchRow]:
    """Batches that currently have birds in the household (``count > 0``), oldest batch first."""
    rows = (
        PoultryMovement.objects.filter(household_id=household_id)
        .values("batch_id", "batch__code", "batch__poultry_type", "batch__breed")
        .annotate(count=_sum(_household_delta()), first_received=Min("date", filter=Q(type=MovementType.DISTRIBUTE)))
        .filter(count__gt=0)
        .order_by("batch_id")
    )
    labels = dict(PoultryType.choices)
    return [
        HouseholdBatchRow(
            batch_id=row["batch_id"],
            batch_code=row["batch__code"],
            poultry_type=row["batch__poultry_type"],
            poultry_type_display=labels[row["batch__poultry_type"]],
            breed=row["batch__breed"],
            count=row["count"],
            first_received_date=row["first_received"],
        )
        for row in rows
    ]

"""Farm business logic: households, batches, poultry movements, daily reports.

Every operation that changes bird counts runs in ``transaction.atomic()`` and locks the
batch row with ``select_for_update()`` before validating quantities.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date as date_type
from decimal import Decimal

from django.db import IntegrityError, transaction
from django.db.models import Max
from django.utils import timezone
from rest_framework.exceptions import PermissionDenied, ValidationError

from accounts import services as account_services
from accounts.models import User
from audit import services as audit
from audit.models import AuditModule
from common.roles import Role, role_of
from notifications import events

from . import selectors
from .models import DailyReport, Household, MovementType, PoultryBatch, PoultryMovement

MSG_FUTURE_DATE = "Sana kelajakda bo‘lishi mumkin emas"
MSG_NO_HOUSEHOLD = "Sizga xonadon biriktirilmagan. Administratorga murojaat qiling."
BATCH_CODE_ATTEMPTS = 5


def _not_enough(available: int, limit_day: date_type | None = None) -> str:
    """``limit_day``: a later day whose end-of-day count would go negative (back-dated movement)."""
    if limit_day is None:
        return f"Yetarli parranda yo‘q. Mavjud: {available}"
    return f"Yetarli parranda yo‘q. Mavjud: {available} ({limit_day:%d.%m.%Y} kuni oxiridagi soni bo‘yicha)"


def validate_not_future(value: date_type, field: str = "date") -> None:
    if value > timezone.localdate():
        raise ValidationError({field: [MSG_FUTURE_DATE]})


def household_of(user: User) -> Household:
    """Household of an OWNER (``PermissionDenied`` if none is attached)."""
    household = Household.objects.select_related("owner").filter(owner=user).first()
    if household is None:
        raise PermissionDenied(MSG_NO_HOUSEHOLD)
    return household


# --- Households -----------------------------------------------------------------------


@dataclass(frozen=True)
class HouseholdInput:
    name: str
    owner_first_name: str
    owner_last_name: str
    owner_username: str
    phone: str
    region: str
    district: str
    mfy: str = ""
    address: str = ""
    latitude: Decimal | None = None
    longitude: Decimal | None = None
    contract_info: str = ""


def create_household(data: HouseholdInput, *, actor: User | None = None) -> tuple[Household, str]:
    """Create the OWNER account and its household atomically. Returns the temp password."""
    try:
        with transaction.atomic():
            owner, password = account_services.create_user(
                username=data.owner_username,
                first_name=data.owner_first_name,
                last_name=data.owner_last_name,
                phone=data.phone,
                role=Role.OWNER,
                actor=actor,
            )
            household = Household.objects.create(
                owner=owner,
                name=data.name,
                phone=data.phone,
                region=data.region,
                district=data.district,
                mfy=data.mfy,
                address=data.address,
                latitude=data.latitude,
                longitude=data.longitude,
                contract_info=data.contract_info,
            )
            audit.record_create(AuditModule.HOUSEHOLDS, household, actor)
            events.household_created(household)
            # Welcome row for the new owner only (admins already get household_created, V3 §9.4).
            events.user_created(owner, notify_admins=False)
    except IntegrityError:
        raise ValidationError({"owner_username": ["Bu login band"]}) from None
    return household, password


HOUSEHOLD_EDITABLE_FIELDS = frozenset(
    {
        "name",
        "phone",
        "region",
        "district",
        "mfy",
        "address",
        "latitude",
        "longitude",
        "is_active",
        "contract_info",
        "is_demo",
    }
)


def update_household(household: Household, changes: dict, *, actor: User | None = None) -> Household:
    """Apply ``changes``; the audit row (``households``/``update``) holds only changed fields."""
    unknown = set(changes) - HOUSEHOLD_EDITABLE_FIELDS
    if unknown:
        raise ValueError(f"Not editable: {sorted(unknown)}")
    with transaction.atomic():
        before = audit.snapshot(household)
        for field, value in changes.items():
            setattr(household, field, value)
        if changes:
            household.save(update_fields=[*changes, "updated_at"])
        audit.record_update(AuditModule.HOUSEHOLDS, household, actor, before)
    return household


def set_household_location(
    household: Household, latitude: Decimal, longitude: Decimal, *, actor: User | None = None
) -> Household:
    return update_household(household, {"latitude": latitude, "longitude": longitude}, actor=actor)


# --- Batches --------------------------------------------------------------------------


def create_batch(*, actor: User, **fields) -> PoultryBatch:
    """Create a batch with code ``NNP-<year>-<seq:03d>`` (sequence restarts every year)."""
    arrived_at: date_type = fields["arrived_at"]
    validate_not_future(arrived_at, "arrived_at")
    year = arrived_at.year
    for _ in range(BATCH_CODE_ATTEMPTS):
        try:
            with transaction.atomic():
                last = PoultryBatch.objects.filter(arrived_at__year=year).aggregate(m=Max("year_seq"))["m"] or 0
                seq = last + 1
                batch = PoultryBatch.objects.create(
                    code=f"NNP-{year}-{seq:03d}", year_seq=seq, created_by=actor, **fields
                )
                audit.record_create(AuditModule.BATCHES, batch, actor)
                events.batch_created(batch)
                return batch
        except IntegrityError:
            continue  # concurrent insert took the same code: recompute
    raise ValidationError({"detail": "Partiya kodini yaratib bo‘lmadi. Qayta urinib ko‘ring."})


BATCH_EDITABLE_FIELDS = frozenset({"health_status", "supplier"})


def update_batch(*, actor: User | None, batch_id: int, changes: dict) -> PoultryBatch:
    """``PATCH /api/batches/{id}``: only ``health_status`` / ``supplier``, under the batch row lock."""
    unknown = set(changes) - BATCH_EDITABLE_FIELDS
    if unknown:
        raise ValueError(f"Not editable: {sorted(unknown)}")
    with transaction.atomic():
        batch = PoultryBatch.objects.select_for_update().get(pk=batch_id)
        before = audit.snapshot(batch)
        changed = [field for field, value in changes.items() if getattr(batch, field) != value]
        for field in changed:
            setattr(batch, field, changes[field])
        if changed:
            batch.save(update_fields=changed)
        audit.record_update(AuditModule.BATCHES, batch, actor, before)
    return batch


# --- Movements ------------------------------------------------------------------------


def _create_movement_locked(
    *,
    actor: User,
    batch_id: int,
    household: Household | None,
    movement_type: str,
    quantity: int,
    on_date: date_type,
    note: str,
    quantity_field: str = "quantity",
    owner_mode: bool = False,
) -> PoultryMovement:
    """Validate and create a movement. Must run inside ``transaction.atomic()``."""
    batch = PoultryBatch.objects.select_for_update().get(pk=batch_id)
    if owner_mode and selectors.household_batch_count(batch_id=batch.pk, household_id=household.pk) <= 0:
        raise ValidationError({"batch": ["Bu partiyadan xonadoningizda parranda yo‘q"]})

    if movement_type in (MovementType.DISTRIBUTE, MovementType.RETURN) and household is None:
        raise ValidationError({"household": ["Xonadonni tanlang"]})
    if movement_type == MovementType.DISTRIBUTE and not household.is_active:
        raise ValidationError({"household": ["Xonadon faol emas"]})
    validate_not_future(on_date)
    if on_date < batch.arrived_at:
        raise ValidationError({"date": ["Sana partiya kelgan sanadan oldin bo‘lishi mumkin emas"]})

    # A movement dated ``on_date`` changes every end-of-day count from ``on_date`` on, so it is checked
    # against the LOWEST count of all those days (not only the current one): a back-dated DEATH / OUT /
    # RETURN / DISTRIBUTE can never make a past count B_h(d) or center_stock(d) negative (V2 §1.1).
    if movement_type == MovementType.DISTRIBUTE or household is None:
        availability = selectors.batch_center_availability(batch, on_date)
    else:
        availability = selectors.household_batch_availability(
            batch_id=batch.pk, household_id=household.pk, on_date=on_date
        )
    if quantity > availability["available"]:
        raise ValidationError(
            {quantity_field: [_not_enough(max(availability["available"], 0), availability["limit_day"])]}
        )

    movement = PoultryMovement.objects.create(
        batch=batch,
        household=household,
        type=movement_type,
        quantity=quantity,
        date=on_date,
        note=note,
        created_by=actor,
    )
    audit.record_create(AuditModule.MOVEMENTS, movement, actor)
    events.movement_recorded(movement)
    return movement


def record_movement(
    *,
    actor: User,
    batch: PoultryBatch,
    household: Household | None,
    movement_type: str,
    quantity: int,
    on_date: date_type | None = None,
    note: str = "",
) -> PoultryMovement:
    """Create a poultry movement. OWNERs may only record DEATH in their own household."""
    owner_mode = role_of(actor) == Role.OWNER
    if owner_mode:
        if movement_type != MovementType.DEATH:
            raise ValidationError({"type": ["Siz faqat nobud bo‘lishni qayd eta olasiz"]})
        household = household_of(actor)
    with transaction.atomic():
        return _create_movement_locked(
            actor=actor,
            batch_id=batch.pk,
            household=household,
            movement_type=movement_type,
            quantity=quantity,
            on_date=on_date or timezone.localdate(),
            note=note,
            owner_mode=owner_mode,
        )


# --- Daily reports --------------------------------------------------------------------


MSG_REPORT_EXISTS = "Bu sana uchun hisobot allaqachon kiritilgan"


def create_daily_report(
    *,
    actor: User,
    household: Household | None,
    batch: PoultryBatch | None = None,
    on_date: date_type | None = None,
    feed_kg: Decimal,
    eggs: int,
    deaths: int = 0,
    meat_kg: Decimal | None = None,
    avg_weight_g: Decimal | None = None,
    note: str = "",
) -> DailyReport:
    """One report per household per day; ``deaths > 0`` also records a DEATH movement."""
    if role_of(actor) == Role.OWNER:
        household = household_of(actor)
    elif household is None:
        raise ValidationError({"household": ["Xonadonni tanlang"]})
    on_date = on_date or timezone.localdate()
    validate_not_future(on_date)

    try:
        with transaction.atomic():
            if DailyReport.objects.filter(household=household, date=on_date).exists():
                raise ValidationError({"date": [MSG_REPORT_EXISTS]})
            death_movement = None
            if deaths > 0:
                if batch is None:
                    # The batch with the most birds that can die on ``on_date`` (back-dated reports too).
                    rows = selectors.household_availability(household_id=household.pk, on_date=on_date)
                    candidates = [(row["available"], -batch_id) for batch_id, row in rows.items()]
                    if not candidates or max(candidates)[0] <= 0:
                        raise ValidationError({"deaths": [_not_enough(0)]})
                    batch_id = -max(candidates)[1]
                else:
                    batch_id = batch.pk
                death_movement = _create_movement_locked(
                    actor=actor,
                    batch_id=batch_id,
                    household=household,
                    movement_type=MovementType.DEATH,
                    quantity=deaths,
                    on_date=on_date,
                    note="Kunlik hisobot",
                    quantity_field="deaths",
                )
            report = DailyReport.objects.create(
                household=household,
                date=on_date,
                bird_count=selectors.household_bird_count(household.pk),
                feed_kg=feed_kg,
                eggs=eggs,
                meat_kg=meat_kg,
                deaths=deaths,
                avg_weight_g=avg_weight_g,
                note=note,
                death_movement=death_movement,
                created_by=actor,
            )
            audit.record_create(AuditModule.DAILY_REPORTS, report, actor)
            events.daily_report_created(report)
            return report
    except IntegrityError:
        # A concurrent request inserted the same (household, date) first.
        raise ValidationError({"date": [MSG_REPORT_EXISTS]}) from None

from __future__ import annotations

from datetime import date, datetime, time
from decimal import Decimal
from unittest import mock
from zoneinfo import ZoneInfo

from django.core.cache import cache
from rest_framework.test import APITestCase

from accounts.models import User
from common.roles import Role
from common.testing import make_household, make_user
from farm import services as farm_services
from farm.models import Household, MovementType, PoultryBatch, PoultryType

TASHKENT = ZoneInfo("Asia/Tashkent")
TODAY = date(2026, 11, 4)
FROZEN_NOW = datetime.combine(TODAY, time(12, 0), tzinfo=TASHKENT)


def local_dt(day: date, hour: int = 9, minute: int = 0) -> datetime:
    return datetime.combine(day, time(hour, minute), tzinfo=TASHKENT)


class FrozenClockTestCase(APITestCase):
    """``timezone.now()`` is fixed at 04.11.2026 12:00 Tashkent for the whole class (incl. setUpTestData)."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.enterClassContext(mock.patch("django.utils.timezone.now", return_value=FROZEN_NOW))
        super().setUpClass()

    def setUp(self) -> None:
        cache.clear()

    def login_as(self, user: User | None) -> None:
        self.client.force_authenticate(user=user)


def batch(actor: User, quantity: int, arrived: date, poultry_type: str = PoultryType.LAYER) -> PoultryBatch:
    return farm_services.create_batch(
        actor=actor,
        poultry_type=poultry_type,
        breed="Lohmann Brown" if poultry_type == PoultryType.LAYER else "Ross 308",
        initial_quantity=quantity,
        age_days=1,
        supplier="Toshkent inkubatori",
        arrived_at=arrived,
    )


def move(actor: User, batch_obj: PoultryBatch, household: Household | None, kind: str, quantity: int, day: date):
    return farm_services.record_movement(
        actor=actor, batch=batch_obj, household=household, movement_type=kind, quantity=quantity, on_date=day
    )


def report(household: Household, day: date, eggs: int, feed: str, deaths: int = 0):
    return farm_services.create_daily_report(
        actor=household.owner, household=household, on_date=day, feed_kg=Decimal(feed), eggs=eggs, deaths=deaths
    )


def created_on(household: Household, day: date) -> None:
    Household.objects.filter(pk=household.pk).update(created_at=local_dt(day))
    household.refresh_from_db()


def worked_example(admin: User) -> dict:
    """§1.3: household created 01.10.2026, LAYER 500 on 20.10, DEATH 4 on 02.11, BROILER 100 on 03.11."""
    household = make_household("Navbahor 12-uy", "owner_xn3")
    created_on(household, date(2026, 10, 1))
    layer = batch(admin, 1000, date(2026, 10, 15))
    broiler = batch(admin, 300, date(2026, 11, 1), PoultryType.BROILER)
    move(admin, layer, household, MovementType.DISTRIBUTE, 500, date(2026, 10, 20))
    move(admin, layer, household, MovementType.DEATH, 4, date(2026, 11, 2))
    move(admin, broiler, household, MovementType.DISTRIBUTE, 100, date(2026, 11, 3))
    report(household, date(2026, 11, 1), 410, "55.00")
    report(household, date(2026, 11, 3), 400, "66.00")
    return {"household": household, "layer": layer, "broiler": broiler}


def staff() -> tuple[User, User]:
    return make_user("admin1", Role.ADMIN), make_user("ombor1", Role.WAREHOUSE)

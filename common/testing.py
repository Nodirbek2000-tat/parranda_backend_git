"""Shared fixtures for the API tests."""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

from django.core.cache import cache
from django.utils import timezone
from rest_framework.test import APITestCase

from accounts.models import User
from common.roles import Role
from farm import services as farm_services
from farm.models import Household, MovementType, PoultryBatch, PoultryType
from warehouse.models import Product, ProductCategory, Unit

BOT_API_TOKEN = "test-bot-api-token"
BOT_TOKEN = "123456:TEST-telegram-bot-token"


def make_user(username: str, role: str, *, password: str = "Parol12345", **extra) -> User:
    user = User(username=username, role=role, first_name=extra.pop("first_name", username.title()), **extra)
    user.set_password(password)
    user.save()
    return user


def make_household(name: str, username: str, **extra) -> Household:
    household, _ = farm_services.create_household(
        farm_services.HouseholdInput(
            name=name,
            owner_first_name=extra.pop("first_name", "Bobur"),
            owner_last_name=extra.pop("last_name", "Aliyev"),
            owner_username=username,
            phone=extra.pop("phone", "+998901234567"),
            region="Samarqand viloyati",
            district="Navobod tumani",
            mfy="Navbahor MFY",
            address="12-uy",
            **extra,
        )
    )
    return household


def make_batch(actor: User, quantity: int = 1000, poultry_type: str = PoultryType.LAYER) -> PoultryBatch:
    return farm_services.create_batch(
        actor=actor,
        poultry_type=poultry_type,
        breed="Lohmann Brown",
        initial_quantity=quantity,
        age_days=1,
        supplier="Toshkent inkubatori",
        arrived_at=timezone.localdate() - timedelta(days=30),
    )


def distribute(actor: User, batch: PoultryBatch, household: Household, quantity: int):
    return farm_services.record_movement(
        actor=actor,
        batch=batch,
        household=household,
        movement_type=MovementType.DISTRIBUTE,
        quantity=quantity,
        on_date=timezone.localdate() - timedelta(days=20),
    )


def make_product(name: str = "Yem PK-1", min_stock: str = "100", unit: str = Unit.KG) -> Product:
    return Product.objects.create(
        name=name, category=ProductCategory.FEED, unit=unit, min_stock=Decimal(min_stock)
    )


class BaseAPITestCase(APITestCase):
    """Admin, warehouse user and two households (A, B) each owning birds of one batch."""

    @classmethod
    def setUpTestData(cls) -> None:
        cls.admin = make_user("admin1", Role.ADMIN)
        cls.warehouse = make_user("ombor1", Role.WAREHOUSE)
        cls.household_a = make_household("Navbahor 12-uy", "owner_a")
        cls.household_b = make_household("Bog‘ishamol 5-uy", "owner_b")
        cls.owner_a = cls.household_a.owner
        cls.owner_b = cls.household_b.owner
        for owner in (cls.owner_a, cls.owner_b):
            owner.set_password("Parol12345")
            owner.must_change_password = False
            owner.save()
        cls.batch = make_batch(cls.admin, 1000)
        distribute(cls.admin, cls.batch, cls.household_a, 300)
        distribute(cls.admin, cls.batch, cls.household_b, 200)

    def setUp(self) -> None:
        cache.clear()  # login throttle counters

    def login_as(self, user: User | None) -> None:
        self.client.force_authenticate(user=user)

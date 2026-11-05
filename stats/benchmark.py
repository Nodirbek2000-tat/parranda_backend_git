"""Synthetic large dataset (bulk_create) and timing helpers for the stats performance check (§7.4).

Used by ``manage.py stats_benchmark`` (throwaway ``DB_PATH`` only) and by the performance test.
Rows are inserted with ``bulk_create`` straight into the tables, so this must never run on real data.
"""

from __future__ import annotations

import random
import statistics
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal

from django.db import connection, models, transaction
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from accounts.models import User
from common.roles import Role
from farm.models import DailyReport, Household, MovementType, PoultryBatch, PoultryMovement, PoultryType
from warehouse.models import Product, ProductCategory, StockTransaction, StockTransactionType, Unit

DISTRICTS = ("Navobod tumani", "Urgut tumani", "Pastdarg‘om tumani", "Jomboy tumani")
BATCH_TYPES = (PoultryType.LAYER,) * 6 + (PoultryType.BROILER,) * 3 + (PoultryType.BREEDER,)


@dataclass(frozen=True)
class DatasetSize:
    households: int = 100
    report_days: int = 730  # 365-day period + its previous period
    products: int = 20
    transactions_per_product_day: int = 10
    stock_days: int = 365


@contextmanager
def _no_auto_now(*fields: models.DateTimeField) -> Iterator[None]:
    """Let ``bulk_create`` keep the given ``created_at`` values (``auto_now_add`` would overwrite them)."""
    saved = [(field, field.auto_now_add) for field in fields]
    for field, _ in saved:
        field.auto_now_add = False
    try:
        yield
    finally:
        for field, value in saved:
            field.auto_now_add = value


def _local(day: date, seconds: int) -> datetime:
    midnight = timezone.make_aware(datetime.combine(day, datetime.min.time()), timezone.get_current_timezone())
    return midnight + timedelta(seconds=seconds)


def build_dataset(size: DatasetSize = DatasetSize(), *, today: date | None = None, seed: int = 2026) -> dict[str, int]:
    """Insert households, batches, ~1 000 movements, reports and stock transactions. Returns row counts."""
    today = today or timezone.localdate()
    rng = random.Random(seed)
    first_day = today - timedelta(days=size.report_days - 1)
    created_day = first_day - timedelta(days=10)
    get = lambda model, name: model._meta.get_field(name)  # noqa: E731

    with transaction.atomic(), _no_auto_now(
        get(Household, "created_at"), get(PoultryBatch, "created_at"), get(PoultryMovement, "created_at"),
        get(DailyReport, "created_at"), get(Product, "created_at"), get(StockTransaction, "created_at"),
    ):
        created = _local(created_day, 9 * 3600)
        actor = User.objects.create(username="bench_admin", role=Role.ADMIN, first_name="Bench")
        owners = User.objects.bulk_create(
            [User(username=f"bench_owner{i}", role=Role.OWNER, first_name="Egasi", password="!") for i in
             range(size.households)]
        )
        households = Household.objects.bulk_create(
            [
                Household(owner=owner, name=f"Bench uy {i}", phone="+998900000000", region="Samarqand viloyati",
                          district=DISTRICTS[i % len(DISTRICTS)], is_active=i % 25 != 0, created_at=created,
                          updated_at=created)
                for i, owner in enumerate(owners)
            ]
        )
        arrived = created_day + timedelta(days=2)
        batches = PoultryBatch.objects.bulk_create(
            [
                PoultryBatch(code=f"BENCH-{i:03d}", year_seq=900 + i, poultry_type=kind, breed="Bench",
                             initial_quantity=40_000, age_days=1, supplier="Bench", arrived_at=arrived,
                             created_by=actor, created_at=created)
                for i, kind in enumerate(BATCH_TYPES)
            ]
        )
        movements: list[PoultryMovement] = []

        def movement(batch, household, kind, quantity, day):
            movements.append(PoultryMovement(batch=batch, household=household, type=kind, quantity=quantity,
                                             date=day, created_by=actor, created_at=_local(day, 39_600)))

        for index, household in enumerate(households):
            layer = batches[index % 6]
            movement(layer, household, MovementType.DISTRIBUTE, 400, arrived + timedelta(days=1))
            if index % 3 == 0:
                movement(batches[6 + index % 3], household, MovementType.DISTRIBUTE, 200, arrived + timedelta(days=3))
            for _ in range(7):  # deaths spread over the whole history
                day = first_day + timedelta(days=rng.randrange(size.report_days))
                movement(layer, household, MovementType.DEATH, rng.randint(1, 3), day)
            if index % 10 == 0:
                movement(layer, household, MovementType.RETURN, 10, first_day + timedelta(days=size.report_days // 2))
        for batch in batches:
            for _ in range(4):
                day = first_day + timedelta(days=rng.randrange(size.report_days))
                movement(batch, None, rng.choice((MovementType.DEATH, MovementType.OUT)), rng.randint(5, 20), day)
        PoultryMovement.objects.bulk_create(movements, batch_size=2000)

        reports: list[DailyReport] = []
        for household in households:
            mixed = household.pk % 3 == 0
            for offset in range(size.report_days):
                if rng.random() < 0.04:
                    continue  # missing report
                day = first_day + timedelta(days=offset)
                eggs = rng.randint(300, 360)
                feed = Decimal(rng.randint(4400, 5200) + (2800 if mixed else 0)).scaleb(-2)
                reports.append(DailyReport(household=household, date=day, bird_count=0, feed_kg=feed, eggs=eggs,
                                           created_by=household.owner, created_at=_local(day, 68_400)))
                if len(reports) >= 5000:
                    DailyReport.objects.bulk_create(reports)
                    reports = []
        DailyReport.objects.bulk_create(reports)

        products = Product.objects.bulk_create(
            [
                Product(name=f"Bench mahsulot {i:02d}",
                        category=ProductCategory.FEED if i < 8 else (ProductCategory.VACCINE, ProductCategory.VET_DRUG,
                                                                     ProductCategory.OTHER)[i % 3],
                        unit=Unit.KG if i < 8 else Unit.PIECE, min_stock=Decimal("100"), created_at=created)
                for i in range(size.products)
            ]
        )
        stock_first = today - timedelta(days=size.stock_days - 1)
        transactions = 0
        rows: list[StockTransaction] = []
        balances = {product.pk: Decimal(0) for product in products}
        step = 86_400 // (size.transactions_per_product_day + 1)
        for offset in range(size.stock_days):
            day = stock_first + timedelta(days=offset)
            for slot in range(size.transactions_per_product_day):
                for product in products:
                    is_in = slot == 0
                    quantity = Decimal(rng.randint(400, 600) if is_in else rng.randint(5, 50))
                    if not is_in and quantity > balances[product.pk]:
                        quantity = balances[product.pk] or Decimal(1)
                        is_in = balances[product.pk] == 0
                    balances[product.pk] += quantity if is_in else -quantity
                    rows.append(StockTransaction(
                        product=product, type=StockTransactionType.IN if is_in else StockTransactionType.OUT,
                        quantity=quantity, balance_after=balances[product.pk],
                        household=None if is_in or slot % 3 == 0 else households[(slot * 7 + offset) % len(households)],
                        document_no="BENCH", created_by=actor, created_at=_local(day, step * (slot + 1)),
                    ))
                    transactions += 1
            if len(rows) >= 5000:
                StockTransaction.objects.bulk_create(rows)
                rows = []
        StockTransaction.objects.bulk_create(rows)
        for product in products:
            Product.objects.filter(pk=product.pk).update(balance=balances[product.pk])
    return {
        "households": len(households),
        "batches": len(batches),
        "movements": len(movements),
        "reports": DailyReport.objects.count(),
        "products": len(products),
        "stock_transactions": transactions,
    }


@dataclass
class Timing:
    name: str
    median_ms: float
    queries: int


def time_call(name: str, call: Callable[[], object], *, runs: int = 5, warmup: int = 1) -> Timing:
    """Median wall time of ``runs`` warm calls and the SQL query count of one call."""
    for _ in range(warmup):
        call()
    with CaptureQueriesContext(connection) as ctx:
        call()
    queries = len(ctx.captured_queries)  # read now: a later request resets the query log
    samples = []
    for _ in range(runs):
        started = time.perf_counter()
        call()
        samples.append((time.perf_counter() - started) * 1000)
    return Timing(name, statistics.median(samples), queries)

"""Two concurrent ``start`` calls post the stock OUT exactly once (API_CONTRACT_V3 §7.4, §19.1)."""

from __future__ import annotations

import threading
from decimal import Decimal

from django.db import OperationalError, connections
from django.test import TransactionTestCase

from common.roles import Role
from common.testing import make_household, make_product, make_user
from logistics import services
from logistics.models import Driver, LogisticsTask, TaskStatus, Vehicle
from warehouse.models import Product, StockTransaction, StockTransactionType

from .helpers import stock_in


class ConcurrentStartTests(TransactionTestCase):
    def setUp(self) -> None:
        self.admin = make_user("admin_c", Role.ADMIN)
        self.logist = make_user("logist_c", Role.LOGISTICS)
        household = make_household("Navbahor 1-uy", "owner_c")
        self.feed = make_product("Yem PK-9", min_stock="0")
        stock_in(self.admin, self.feed, "500")
        vehicle = Vehicle.objects.create(plate_number="01 C 100 CC")
        driver = Driver.objects.create(full_name="Sardor Rahimov", phone="+998901112233", user=self.logist)
        task = services.create_task(
            actor=self.admin,
            household=household,
            items=[services.ItemInput(product=self.feed, quantity=Decimal("120"))],
            vehicle=vehicle.pk,
            driver=driver.pk,
        )
        services.accept(actor=self.logist, task_id=task.pk)
        self.task_id = task.pk

    def test_two_threads_one_out(self):
        barrier = threading.Barrier(2)
        outcomes: list[str] = []
        lock = threading.Lock()

        def worker() -> None:
            try:
                barrier.wait(timeout=10)
                services.start(actor=self.logist, task_id=self.task_id)
                result = "ok"
            except services.Conflict:
                result = "conflict"
            except OperationalError:  # shared in-memory test DB: the loser may see "table is locked"
                result = "locked"
            finally:
                connections.close_all()
            with lock:
                outcomes.append(result)

        threads = [threading.Thread(target=worker) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=60)

        self.assertEqual(len(outcomes), 2, outcomes)
        self.assertEqual(outcomes.count("ok"), 1, outcomes)
        self.assertIn(next(result for result in outcomes if result != "ok"), {"conflict", "locked"})
        outs = StockTransaction.objects.filter(reference_id=self.task_id, type=StockTransactionType.OUT)
        self.assertEqual(outs.count(), 1)
        feed = Product.objects.get(pk=self.feed.pk)
        self.assertEqual((feed.balance, feed.reserved), (Decimal("380"), Decimal("0")))
        self.assertEqual(LogisticsTask.objects.get(pk=self.task_id).status, TaskStatus.IN_TRANSIT)

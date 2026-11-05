"""Seed a THROWAWAY SQLite database with 100 households × 730 days of reports and time every stats endpoint.

    set DB_PATH=C:\\temp\\nnp_bench.sqlite3
    venv\\Scripts\\python.exe manage.py stats_benchmark

Refuses to run unless ``DB_PATH`` points to a file other than ``db.sqlite3`` (rows are bulk inserted).
Prints a table (median of 5 warm runs, SQL queries per call) and exits with code 1 if any endpoint
(default period and full 366-day period) has a median ≥ 300 ms (API_CONTRACT_V2 §7.4).
"""

from __future__ import annotations

import os
from datetime import timedelta
from pathlib import Path

from django.conf import settings
from django.core.management import call_command
from django.core.management.base import BaseCommand, CommandError
from django.test.utils import override_settings
from django.utils import timezone
from rest_framework.test import APIRequestFactory, force_authenticate

from accounts.models import User
from common.roles import Role
from farm import selectors
from farm.models import Household
from farm.serializers import BatchSerializer
from farm.views import BatchViewSet, HouseholdViewSet
from stats import services
from stats.benchmark import DatasetSize, Timing, build_dataset, time_call
from stats.views import BotSummaryView, MyStatsView, OverviewStatsView, WarehouseStatsView
from warehouse.models import Product, ProductCategory
from warehouse.serializers import ProductSerializer
from warehouse.views import ProductViewSet

TARGET_MS = 300.0
BENCH_BOT_TOKEN = "bench-bot-token"


def _guard() -> Path:
    raw = os.environ.get("DB_PATH", "").strip()
    if not raw or os.environ.get("DATABASE_URL"):
        raise CommandError("Faqat bir martalik SQLite bazada ishlaydi: DB_PATH=<boshqa fayl> o‘rnating.")
    target = Path(raw).resolve()
    if target == (settings.BASE_DIR / "db.sqlite3").resolve():
        raise CommandError("DB_PATH asosiy db.sqlite3 ga teng — benchmark faqat bir martalik nusxada ishlaydi.")
    configured = Path(str(settings.DATABASES["default"]["NAME"])).resolve()
    if configured != target:
        raise CommandError("DATABASES NAME DB_PATH ga mos emas.")
    return target


class Command(BaseCommand):
    help = "Statistika endpointlari tezligini bir martalik bazada o‘lchaydi (faqat DB_PATH bilan)."

    def add_arguments(self, parser) -> None:
        parser.add_argument("--reuse", action="store_true", help="Use the already seeded benchmark database.")
        parser.add_argument("--runs", type=int, default=5)
        parser.add_argument("--households", type=int, default=100)
        parser.add_argument("--report-days", type=int, default=730)

    def handle(self, *args, reuse: bool, runs: int, households: int, report_days: int, **options) -> None:
        target = _guard()
        call_command("migrate", verbosity=0, interactive=False)
        if Household.objects.exists():
            if not reuse:
                raise CommandError(f"{target} bo‘sh emas. Yangi bir martalik fayl bering yoki --reuse ishlating.")
        else:
            counts = build_dataset(DatasetSize(households=households, report_days=report_days))
            self.stdout.write("Yaratildi: " + ", ".join(f"{key}={value}" for key, value in counts.items()))
        with override_settings(BOT_API_TOKEN=BENCH_BOT_TOKEN):
            timings = self.measure(runs)
        self.stdout.write(f"\n{'endpoint':<44}{'median ms':>10}{'queries':>9}")
        failed = []
        for timing in timings:
            mark = ""
            if timing.median_ms >= TARGET_MS:
                failed.append(timing.name)
                mark = "  ← ≥ 300 ms"
            self.stdout.write(f"{timing.name:<44}{timing.median_ms:>10.1f}{timing.queries:>9}{mark}")
        if failed:
            raise CommandError(f"{len(failed)} ta endpoint 300 ms dan sekin: {', '.join(failed)}", returncode=1)
        self.stdout.write(self.style.SUCCESS("\nBarcha standart davrdagi endpointlar < 300 ms."))

    def measure(self, runs: int) -> list[tuple[Timing, bool]]:
        today = timezone.localdate()
        year = {"date_from": (today - timedelta(days=365)).isoformat()}  # 366 days (maximum)
        factory = APIRequestFactory()
        admin = User.objects.filter(role=Role.ADMIN).first() or User.objects.create(username="bench_viewer",
                                                                                     role=Role.ADMIN)
        household = Household.objects.select_related("owner").order_by("id").first()
        owner = household.owner
        if owner.telegram_id is None:
            User.objects.filter(pk=owner.pk).update(telegram_id=990_000_001)
            owner.telegram_id = 990_000_001
        if admin.telegram_id is None:
            User.objects.filter(pk=admin.pk).update(telegram_id=990_000_002)
            admin.telegram_id = 990_000_002
        batch = selectors.batches_with_counts().order_by("id").first()
        product = Product.objects.filter(category=ProductCategory.FEED).order_by("id").first()

        def view(view_func, path: str, user=None, headers=None, **kwargs):
            def call():
                request = factory.get(path, **(headers or {}))
                if user is not None:
                    force_authenticate(request, user=user)
                response = view_func(request, **kwargs)
                response.render()
                assert response.status_code == 200, (path, response.status_code, response.content[:300])
                return response

            return call

        overview = OverviewStatsView.as_view()
        household_view = HouseholdViewSet.as_view({"get": "stats"})
        batch_view = BatchViewSet.as_view({"get": "stats"})
        product_view = ProductViewSet.as_view({"get": "stats"})
        warehouse_view = WarehouseStatsView.as_view()
        my_view = MyStatsView.as_view()
        bot_view = BotSummaryView.as_view()
        bot_headers = {"HTTP_X_BOT_TOKEN": BENCH_BOT_TOKEN}
        year_query = f"?date_from={year['date_from']}"
        plan = [
            ("service overview (30 d)", lambda: services.overview({})),
            ("service overview (366 d)", lambda: services.overview(year)),
            ("service household (30 d)", lambda: services.household_stats(household, {})),
            ("service household (366 d)", lambda: services.household_stats(household, year)),
            ("service batch (life)", lambda: services.batch_stats(batch, BatchSerializer(batch).data, {})),
            ("service product (30 d)", lambda: services.product_stats(product, ProductSerializer(product).data, {})),
            ("service product (366 d)",
             lambda: services.product_stats(product, ProductSerializer(product).data, year)),
            ("service warehouse (30 d)", lambda: services.warehouse_stats({})),
            ("service warehouse (366 d)", lambda: services.warehouse_stats(year)),
            ("service bot summary owner", lambda: services.bot_summary(owner)),
            ("service bot summary staff", lambda: services.bot_summary(admin)),
            ("GET /api/stats/overview", view(overview, "/api/stats/overview", admin)),
            ("GET /api/stats/overview (366 d)", view(overview, f"/api/stats/overview{year_query}", admin)),
            ("GET /api/households/{id}/stats",
             view(household_view, f"/api/households/{household.pk}/stats", admin, pk=str(household.pk))),
            ("GET /api/my/stats", view(my_view, "/api/my/stats", owner)),
            ("GET /api/batches/{id}/stats", view(batch_view, f"/api/batches/{batch.pk}/stats", admin,
                                                 pk=str(batch.pk))),
            ("GET /api/products/{id}/stats",
             view(product_view, f"/api/products/{product.pk}/stats", admin, pk=str(product.pk))),
            ("GET /api/products/{id}/stats (366 d)",
             view(product_view, f"/api/products/{product.pk}/stats{year_query}", admin, pk=str(product.pk))),
            ("GET /api/warehouse/stats", view(warehouse_view, "/api/warehouse/stats", admin)),
            ("GET /api/warehouse/stats (366 d)", view(warehouse_view, f"/api/warehouse/stats{year_query}", admin)),
            ("GET /api/bot/users/{id}/summary (owner)",
             view(bot_view, "/x", headers=bot_headers, telegram_id=owner.telegram_id)),
            ("GET /api/bot/users/{id}/summary (staff)",
             view(bot_view, "/x", headers=bot_headers, telegram_id=admin.telegram_id)),
        ]
        return [time_call(name, call, runs=runs) for name, call in plan]

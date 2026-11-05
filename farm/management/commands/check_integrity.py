"""Recompute bird counts and stock balances from the operation rows and report every inconsistency.

Exit code ``0`` when everything matches, ``1`` when at least one problem is found (usable in cron / CI).
Read-only: the command never changes data.
"""

from __future__ import annotations

from collections import defaultdict
from decimal import Decimal

from django.core.management.base import BaseCommand, CommandError
from django.db.models import F, Q, Sum
from django.utils import timezone

from farm import selectors
from farm.selectors import _center_delta, _household_delta
from farm.models import DailyReport, Household, MovementType, PoultryBatch, PoultryMovement
from warehouse.models import OUTGOING_TYPES, Product, StockTransaction, StockTransactionType


class Command(BaseCommand):
    help = (
        "Partiya/xonadon parranda sonlari va ombor qoldiqlarini operatsiyalardan qayta hisoblab, "
        "nomuvofiqliklarni chiqaradi (muammo bo‘lsa chiqish kodi 1)."
    )

    def handle(self, *args, **options) -> None:
        self.problems: list[str] = []
        self.warnings: list[str] = []
        today = timezone.localdate()
        batches = self._check_batches(today)
        self._check_history()
        households = self._check_households()
        reports = self._check_reports(today)
        products, transactions = self._check_products()
        lots = self._check_lots()
        self._check_logistics()
        self._check_vet()
        self.stdout.write(
            f"Tekshirildi: {batches} partiya, {households} xonadon, {reports} kunlik hisobot, "
            f"{products} mahsulot, {transactions} ombor operatsiyasi."
        )
        self.stdout.write(f"Mahsulot partiyalari: {lots}.")
        for warning in self.warnings:
            self.stdout.write(self.style.WARNING(f"! {warning}"))
        if self.problems:
            for problem in self.problems:
                self.stdout.write(self.style.ERROR(f"- {problem}"))
            raise CommandError(f"{len(self.problems)} ta nomuvofiqlik topildi.", returncode=1)
        self.stdout.write(self.style.SUCCESS("Nomuvofiqlik topilmadi."))

    def problem(self, text: str) -> None:
        self.problems.append(text)

    def _check_logistics(self) -> None:
        """V3 §16.3: reservations = active task items, started tasks have their OUTs, order/task statuses."""
        from logistics import integrity
        from logistics.models import LogisticsTask, LogisticsTaskItem
        from warehouse.readiness import models_ready

        if models_ready(LogisticsTask, LogisticsTaskItem):
            for text in integrity.problems():
                self.problem(text)

    def _check_vet(self) -> None:
        """V3 §16.3 (vet team): DONE vaccinations / treatments with a used product have their OUT, etc."""
        from vet import integrity
        from vet.selectors import tables_ready

        if tables_ready():
            for text in integrity.problems():
                self.problem(text)

    # --- poultry --------------------------------------------------------------------------

    def _check_batches(self, today) -> int:
        sums: dict[int, dict[str, int]] = defaultdict(lambda: defaultdict(int))
        per_household: dict[tuple[int, int], int] = defaultdict(int)
        rows = (
            PoultryMovement.all_objects.order_by()
            .values("batch_id", "household_id", "type")
            .annotate(quantity=Sum("quantity"))
            .values_list("batch_id", "household_id", "type", "quantity")
        )
        for batch_id, household_id, movement_type, quantity in rows:
            place = "center" if household_id is None else "households"
            sums[batch_id][f"{movement_type}:{place}"] += quantity
            if household_id is not None:
                sign = 1 if movement_type == MovementType.DISTRIBUTE else -1
                per_household[(batch_id, household_id)] += sign * quantity
        # ``all_objects``: soft-deleted batches keep their movements and must still add up.
        annotated = {batch.pk: batch for batch in selectors.batches_with_counts(PoultryBatch.all_objects.all())}
        for batch_id, batch in annotated.items():
            s = sums[batch_id]
            distributed = s["DISTRIBUTE:households"]
            returned = s["RETURN:households"]
            deaths = s["DEATH:center"] + s["DEATH:households"]
            out = s["OUT:center"] + s["OUT:households"]
            center = batch.initial_quantity - distributed + returned - s["DEATH:center"] - s["OUT:center"]
            in_households = sum(count for (b, _h), count in per_household.items() if b == batch_id)
            label = f"Partiya {batch.code}"
            if center < 0:
                self.problem(f"{label}: markazdagi soni manfiy ({center}).")
            if center != batch.center_stock:
                self.problem(f"{label}: center_stock {batch.center_stock} ≠ qayta hisoblangan {center}.")
            if batch.deaths_total != deaths:
                self.problem(f"{label}: deaths_total {batch.deaths_total} ≠ qayta hisoblangan {deaths}.")
            if batch.distributed_total != distributed - returned:
                self.problem(f"{label}: distributed_total {batch.distributed_total} ≠ {distributed - returned}.")
            if batch.initial_quantity != center + in_households + deaths + out:
                self.problem(
                    f"{label}: boshlang‘ich {batch.initial_quantity} ≠ markaz {center} + xonadonlar {in_households}"
                    f" + nobud {deaths} + chiqim {out}."
                )
        for (batch_id, household_id), count in sorted(per_household.items()):
            if count < 0:
                code = annotated[batch_id].code if batch_id in annotated else batch_id
                self.problem(f"Partiya {code}, xonadon XN-{household_id:04d}: soni manfiy ({count}).")
        early = (
            PoultryMovement.all_objects.filter(Q(date__gt=today) | Q(date__lt=F("batch__arrived_at")))
            .order_by("id")
            .values_list("id", "date", "batch__code")
        )
        for movement_id, day, code in early:
            self.problem(f"Harakat TR-{movement_id:06d} ({day:%d.%m.%Y}): sana kelajakda yoki partiya {code} kelishidan oldin.")
        transfers = PoultryMovement.all_objects.filter(
            type__in=[MovementType.DISTRIBUTE, MovementType.RETURN], household__isnull=True
        ).values_list("id", flat=True)
        for movement_id in transfers:
            self.problem(f"Harakat TR-{movement_id:06d}: tarqatish/qaytarishda xonadon yo‘q.")
        return len(annotated)

    def _check_history(self) -> None:
        """Warnings (exit code unchanged): counts that were negative at the end of some past day.

        Phase 1 validated a DEATH / OUT / RETURN / DISTRIBUTE only against the *current* count, so a
        back-dated movement could make a past end-of-day count negative. Since Phase 2 the service checks
        every end-of-day count from the movement date on (``farm.selectors.*_availability``), so such rows
        can only come from data written before that fix. Current totals stay correct, but the statistics
        reconstruct past days from these movements and would show negative birds there.
        """
        rows = (
            PoultryMovement.all_objects.order_by()
            .values("batch_id", "batch__code", "batch__initial_quantity", "household_id", "date")
            .annotate(household_net=Sum(_household_delta()), center_net=Sum(_center_delta()))
            .order_by("batch_id", "household_id", "date")
            .values_list("batch_id", "batch__code", "batch__initial_quantity", "household_id", "date",
                         "household_net", "center_net")
        )
        running: dict[tuple[int, int], int] = defaultdict(int)
        flagged: set[tuple[int, int]] = set()
        center_days: dict[int, dict] = defaultdict(lambda: defaultdict(int))
        meta: dict[int, tuple[str, int]] = {}
        for batch_id, code, initial, household_id, day, household_net, center_net in rows:
            meta[batch_id] = (code, initial)
            center_days[batch_id][day] += center_net or 0
            if household_id is None:
                continue
            key = (batch_id, household_id)
            running[key] += household_net or 0
            if running[key] < 0 and key not in flagged:
                flagged.add(key)
                self.warnings.append(
                    f"Partiya {code}, xonadon XN-{household_id:04d}: {day:%d.%m.%Y} kuni oxirida soni manfiy "
                    f"({running[key]}) — orqa sana bilan kiritilgan harakat; statistikada o‘tgan kunlar noto‘g‘ri."
                )
        for batch_id, days in center_days.items():
            code, initial = meta[batch_id]
            stock = initial
            for day in sorted(days):
                stock += days[day]
                if stock < 0:
                    self.warnings.append(
                        f"Partiya {code}: {day:%d.%m.%Y} kuni oxirida markazdagi soni manfiy ({stock}) — "
                        "orqa sana bilan kiritilgan harakat."
                    )
                    break

    def _check_households(self) -> int:
        count = 0
        for household in selectors.households_with_counts(Household.all_objects.all()).order_by("id"):
            count += 1
            if household.bird_count < 0:
                self.problem(f"Xonadon {household.code}: parranda soni manfiy ({household.bird_count}).")
        return count

    def _check_reports(self, today) -> int:
        count = 0
        reports = DailyReport.objects.select_related("death_movement").order_by("id")
        for report in reports.iterator(chunk_size=2000):
            count += 1
            label = f"Hisobot {report.op_id} ({report.date:%d.%m.%Y})"
            if report.date > today:
                self.problem(f"{label}: sana kelajakda.")
            movement = report.death_movement
            if report.deaths > 0 and movement is None:
                self.problem(f"{label}: {report.deaths} nobud, lekin DEATH harakati yo‘q.")
            elif report.deaths == 0 and movement is not None:
                self.problem(f"{label}: nobud 0, lekin {movement.op_id} bog‘langan.")
            elif movement is not None and (
                movement.type != MovementType.DEATH
                or movement.quantity != report.deaths
                or movement.household_id != report.household_id
                or movement.date != report.date
            ):
                self.problem(f"{label}: bog‘langan {movement.op_id} hisobotga mos emas.")
        return count

    # --- stock ----------------------------------------------------------------------------

    def _check_products(self) -> tuple[int, int]:
        # ``all_objects``: soft-deleted products keep their stock history and must still add up.
        products = {product.pk: product for product in Product.all_objects.order_by("id")}
        running: dict[int, Decimal] = defaultdict(Decimal)
        transactions = 0
        rows = (
            StockTransaction.objects.order_by("product_id", "id")
            .values_list("id", "product_id", "type", "quantity", "balance_after")
            .iterator(chunk_size=5000)
        )
        for tx_id, product_id, tx_type, quantity, balance_after in rows:
            transactions += 1
            label = f"Operatsiya OM-{tx_id:06d}"
            if quantity <= 0:
                self.problem(f"{label}: miqdor musbat emas ({quantity}).")
            before = running[product_id]
            # V3 §5.3: ADJUST_OUT leaves stock like OUT, ADJUST_IN adds like IN.
            after = before - quantity if tx_type in OUTGOING_TYPES else before + quantity
            if after < 0:
                self.problem(f"{label}: qoldiq manfiyga tushadi ({after}).")
            if after != balance_after:
                self.problem(f"{label}: balance_after {balance_after} ≠ qayta hisoblangan {after}.")
            running[product_id] = after
        for product_id, product in products.items():
            expected = running.get(product_id, Decimal(0))
            if product.balance != expected:
                self.problem(
                    f"Mahsulot “{product.name}”: qoldiq {product.balance} ≠ operatsiyalardan {expected}."
                )
        orphans = StockTransaction.objects.filter(~Q(type=StockTransactionType.OUT), household__isnull=False)
        for tx_id in orphans.values_list("id", flat=True):
            self.problem(f"Operatsiya OM-{tx_id:06d}: xonadon faqat chiqimda bo‘ladi.")
        return len(products), transactions

    # --- V3 lots and reservations (API_CONTRACT_V3 §16.3) ------------------------------------

    def _check_lots(self) -> int:
        """``remaining = received − Σ allocations``, ``0 ≤ Σ remaining ≤ balance``, one lot per IN."""
        from warehouse.models import StockLot, StockLotAllocation
        from warehouse.readiness import models_ready

        if not models_ready(StockLot, StockLotAllocation):
            self.warnings.append("Partiya jadvallari hali yaratilmagan (migrate) — partiyalar tekshirilmadi.")
            return 0
        allocated = dict(
            StockLotAllocation.objects.order_by()
            .values("lot_id")
            .annotate(total=Sum("quantity"))
            .values_list("lot_id", "total")
        )
        tracked: dict[int, Decimal] = defaultdict(Decimal)
        count = 0
        for lot_id, product_id, lot_no, received, remaining in StockLot.objects.order_by("id").values_list(
            "id", "product_id", "lot_no", "received_qty", "remaining_qty"
        ):
            count += 1
            label = f"Mahsulot partiyasi #{lot_id}" + (f" ({lot_no})" if lot_no else "")
            expected = received - allocated.get(lot_id, Decimal(0))
            if remaining != expected:
                self.problem(f"{label}: qoldiq {remaining} ≠ qabul {received} − sarf = {expected}.")
            if remaining < 0 or remaining > received:
                self.problem(f"{label}: qoldiq {remaining} 0 … {received} oralig‘ida emas.")
            tracked[product_id] += remaining
        for product_id, name, balance in Product.all_objects.order_by("id").values_list("id", "name", "balance"):
            total = tracked.get(product_id, Decimal(0))
            if total > balance:
                self.problem(f"Mahsulot “{name}”: partiyalar qoldig‘i {total} > ombor qoldig‘i {balance}.")
        lotless = StockLot.objects.exclude(in_transaction__type=StockTransactionType.IN)
        for lot_id in lotless.values_list("id", flat=True):
            self.problem(f"Mahsulot partiyasi #{lot_id}: kirim operatsiyasiga bog‘lanmagan.")
        wrong = StockLotAllocation.objects.exclude(transaction__type__in=list(OUTGOING_TYPES))
        for alloc_id in wrong.values_list("id", flat=True):
            self.problem(f"Partiyadan sarf #{alloc_id}: chiqim bo‘lmagan operatsiyaga bog‘langan.")
        mismatched = (
            StockTransaction.objects.filter(type__in=list(OUTGOING_TYPES), lot_allocations__isnull=False)
            .order_by()
            .values("id", "product_id", "quantity")
            .annotate(allocated=Sum("lot_allocations__quantity"))
            .filter(allocated__gt=F("quantity"))
        )
        for row in mismatched:
            self.problem(f"Operatsiya OM-{row['id']:06d}: partiyalardan sarf {row['allocated']} > miqdor {row['quantity']}.")
        return count

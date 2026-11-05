"""Fill an empty database with clearly marked DEMO data (≈60 days of history for the statistics pages).

Everything goes through the business services (same validation and counting rules as the API), then
creation timestamps are moved back in time so the history looks real. Passwords are random and written
only to ``demo_credentials.txt`` (gitignored). Management commands write no audit rows.
"""

from __future__ import annotations

import random
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from decimal import Decimal

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone

from accounts.models import User
from accounts.services import generate_temp_password
from common.roles import Role
from farm import selectors
from farm import services as farm_services
from farm.models import DailyReport, Household, MovementType, PoultryBatch, PoultryMovement, PoultryType
from notifications.models import Notification
from warehouse import services as warehouse_services
from warehouse.models import Product, ProductCategory, StockTransaction, Unit

CREDENTIALS_FILE = settings.BASE_DIR / "demo_credentials.txt"
DEMO_NOTE = "DEMO ma’lumot — faqat sinov uchun"
DEMO_STAFF = ("demo_admin", "demo_omborchi", "demo_logist", "demo_vet")
HISTORY_DAYS = 60
REGION = "Samarqand viloyati"


@dataclass(frozen=True)
class HouseholdSpec:
    username: str
    first: str
    last: str
    name: str
    district: str
    mfy: str
    lat: str
    lon: str
    main: int  # broilers distributed from the main batches (A / B)
    extra: int  # broilers of a second, later batch (C / D)
    fcr: float  # typical feed conversion ratio (kg of feed per kg of meat)
    miss_rate: float  # share of days without a report
    death_rate: float  # daily probability of a death


HOUSEHOLDS = [
    HouseholdSpec("demo_egasi1", "Bobur", "Aliyev", "Navbahor 12-uy", "Navobod tumani", "Navbahor MFY",
                  "39.654120", "66.958830", 600, 200, 1.62, 0.02, 0.10),
    HouseholdSpec("demo_egasi2", "Dilnoza", "Karimova", "Bog‘ishamol 5-uy", "Navobod tumani", "Bog‘ishamol MFY",
                  "39.631240", "66.926410", 500, 0, 1.71, 0.05, 0.08),
    HouseholdSpec("demo_egasi3", "Sherzod", "Rahimov", "Yangiobod 27-uy", "Navobod tumani", "Yangiobod MFY",
                  "39.682510", "66.981730", 0, 700, 1.78, 0.04, 0.15),
    HouseholdSpec("demo_egasi4", "Malika", "Yusupova", "Gulobod 3-uy", "Navobod tumani", "Gulobod MFY",
                  "39.661870", "66.944020", 450, 0, 1.80, 0.10, 0.07),
    HouseholdSpec("demo_egasi5", "Otabek", "Saidov", "Chinor 41-uy", "Navobod tumani", "Chinor MFY",
                  "39.645330", "66.990150", 400, 150, 1.86, 0.06, 0.10),
    HouseholdSpec("demo_egasi6", "Nodira", "Ergasheva", "Mehnat 8-uy", "Navobod tumani", "Mehnat MFY",
                  "39.672040", "66.920560", 350, 0, 2.45, 0.20, 0.30),
    HouseholdSpec("demo_egasi7", "Jamshid", "Qodirov", "Urgut markaz 15-uy", "Urgut tumani", "Markaz MFY",
                  "39.402310", "67.243870", 550, 0, 1.58, 0.03, 0.06),
    HouseholdSpec("demo_egasi8", "Feruza", "Tosheva", "Kamangaron 9-uy", "Urgut tumani", "Kamangaron MFY",
                  "39.441120", "67.204550", 0, 600, 1.83, 0.08, 0.12),
    HouseholdSpec("demo_egasi9", "Ulug‘bek", "Nazarov", "Qoratepa 22-uy", "Urgut tumani", "Qoratepa MFY",
                  "39.420870", "67.281400", 300, 200, 1.74, 0.07, 0.09),
    HouseholdSpec("demo_egasi10", "Gulnora", "Hamidova", "Juma 4-uy", "Pastdarg‘om tumani", "Juma MFY",
                  "39.711030", "66.661220", 500, 0, 1.66, 0.04, 0.08),
    HouseholdSpec("demo_egasi11", "Akmal", "Murodov", "Chordara 30-uy", "Pastdarg‘om tumani", "Chordara MFY",
                  "39.735540", "66.703910", 250, 300, 1.90, 0.09, 0.10),
    HouseholdSpec("demo_egasi12", "Sevara", "Islomova", "Payariq yo‘li 2-uy", "Pastdarg‘om tumani", "Do‘stlik MFY",
                  "39.689940", "66.735680", 400, 0, 1.75, 0.05, 0.08),
]
#: This household stops working 12 days before today (``is_active = False``).
INACTIVE_USERNAME = "demo_egasi12"
INACTIVE_FOR_DAYS = 12


def local_dt(day: date, hour: int, minute: int = 0) -> datetime:
    return timezone.make_aware(datetime.combine(day, time(hour, minute)), timezone.get_current_timezone())


class Command(BaseCommand):
    help = (
        "Demo ma’lumotlarni yaratadi (bo‘sh bazada): 12 xonadon, 5 partiya, ~60 kunlik hisobot va ombor tarixi. "
        "--reset barcha biznes ma’lumotlarini o‘chirib qayta yaratadi."
    )

    def add_arguments(self, parser) -> None:
        parser.add_argument(
            "--reset",
            action="store_true",
            help="Delete ALL households, batches, movements, reports, products, stock operations, "
            "notifications and demo users first (audit rows are immutable and stay).",
        )

    def handle(self, *args, reset: bool = False, **options) -> None:
        if Household.all_objects.exists() and not reset:
            raise CommandError(
                "Bazada xonadonlar mavjud. Demo ma’lumotlar faqat bo‘sh bazaga yoziladi "
                "(hammasini o‘chirib qayta yaratish uchun: --reset)."
            )
        with transaction.atomic():
            if reset:
                self._wipe()
            credentials = self._seed()
        lines = [
            "NNP demo hisoblari (faqat lokal sinov uchun, hech kimga yubormang).",
            "",
            *(f"{role:<10} {username:<16} {password}" for role, username, password in credentials),
            "",
        ]
        CREDENTIALS_FILE.write_text("\n".join(lines), encoding="utf-8")
        self.stdout.write(str(CREDENTIALS_FILE))

    # --- reset ----------------------------------------------------------------------

    def _wipe(self) -> None:
        # ``all_objects``: soft-deleted households, products, vehicles and drivers are wiped too.
        owner_ids = list(Household.all_objects.values_list("owner_id", flat=True))
        Notification.objects.all().delete()
        # V3 modules (orders, logistics, vet, files) reference households, products and stock operations.
        from core.models import Attachment
        from logistics.models import Driver, LogisticsTask, Vehicle
        from orders.models import Order
        from vet.models import DiseaseRecord, Treatment, Vaccination, VetRequest, VetVisit

        Treatment.objects.all().delete()
        DiseaseRecord.objects.all().delete()
        Vaccination.objects.all().delete()
        VetVisit.objects.all().delete()
        VetRequest.objects.all().delete()
        LogisticsTask.objects.all().delete()  # items and status logs cascade
        Order.objects.all().delete()
        Driver.all_objects.all().delete()
        Vehicle.all_objects.all().delete()
        for attachment in Attachment.objects.all():
            attachment.file.delete(save=False)
            attachment.delete()
        DailyReport.objects.all().delete()
        # V3 warehouse rows that PROTECT stock operations (lots, allocations, inventory adjustments).
        from warehouse.models import InventoryCount, StockLot, StockLotAllocation

        InventoryCount.objects.all().delete()  # lines cascade
        StockLotAllocation.objects.all().delete()
        StockLot.objects.all().delete()
        StockTransaction.objects.all().delete()
        PoultryMovement.objects.all().delete()
        Product.all_objects.all().delete()
        PoultryBatch.objects.all().delete()
        Household.all_objects.all().delete()
        User.objects.filter(pk__in=owner_ids).delete()
        User.objects.filter(username__in=DEMO_STAFF).delete()

    # --- seed -----------------------------------------------------------------------

    def _seed(self) -> list[tuple[str, str, str]]:
        self.today = timezone.localdate()
        self.start = self.today - timedelta(days=HISTORY_DAYS)
        self.rng = random.Random(2026)
        credentials: list[tuple[str, str, str]] = []

        self.admin = self._staff("demo_admin", "Ali", "Valiyev", "+998901110001", Role.ADMIN, credentials)
        self.warehouse = self._staff("demo_omborchi", "Jasur", "Tursunov", "+998901110002", Role.WAREHOUSE, credentials)

        households: list[tuple[HouseholdSpec, Household]] = []
        for index, spec in enumerate(HOUSEHOLDS):
            household, password = farm_services.create_household(
                farm_services.HouseholdInput(
                    name=spec.name,
                    owner_first_name=spec.first,
                    owner_last_name=spec.last,
                    owner_username=spec.username,
                    phone=f"+9989011100{index + 10:02d}",
                    region=REGION,
                    district=spec.district,
                    mfy=spec.mfy,
                    address=f"{spec.name.rsplit(' ', 1)[0]} ko‘chasi, {spec.name.rsplit(' ', 1)[-1]}",
                    latitude=Decimal(spec.lat),
                    longitude=Decimal(spec.lon),
                    contract_info=DEMO_NOTE,
                )
            )
            created = local_dt(self.start - timedelta(days=8 + index % 4), 10, 15)
            Household.objects.filter(pk=household.pk).update(created_at=created, updated_at=created)
            Household.objects.filter(pk=household.pk).update(is_demo=True)
            User.objects.filter(pk=household.owner_id).update(must_change_password=False, date_joined=created)
            credentials.append((Role.OWNER, spec.username, password))
            households.append((spec, household))

        batches = self._seed_batches()
        self._seed_flock(households, batches)
        self._seed_warehouse([household for _, household in households])
        inactive = next(household for spec, household in households if spec.username == INACTIVE_USERNAME)
        Household.objects.filter(pk=inactive.pk).update(is_active=False)
        active = [household for spec, household in households if spec.username != INACTIVE_USERNAME]
        self._seed_v3(active, credentials)
        return credentials

    # --- V3 modules (TZ §8, §9, §18): logistics, orders, veterinary, lots ---------------------

    def _seed_v3(self, households: list[Household], credentials: list) -> None:
        from django.core.files.uploadedfile import SimpleUploadedFile

        from core import files
        from core.models import AttachmentPurpose
        from logistics import services as logistics
        from logistics.models import Driver, Vehicle
        from orders import services as orders
        from orders.services import ItemInput as OrderItemInput
        from vet import services as vet

        logist = self._staff("demo_logist", "Sardor", "Rahimov", "+998901110003", Role.LOGISTICS, credentials)
        vet_user = self._staff("demo_vet", "Dilshod", "Ergashev", "+998901110004", Role.VET, credentials)
        vehicle = Vehicle.objects.create(plate_number="30 A 777 NN", model="Isuzu NPR (demo)", capacity_kg=Decimal("3000"))
        Vehicle.objects.create(plate_number="30 B 123 NN", model="Damas (demo)", capacity_kg=Decimal("500"))
        driver = Driver.objects.create(full_name="Sardor Rahimov", phone="+998901110003", user=logist)

        # Vaccine and drug with lots (one expiring soon) — FEFO, expiry reminders, vet stock OUTs.
        vaccine = Product.objects.create(name="Nyukasl vaksinasi (demo)", category=ProductCategory.VACCINE,
                                         unit=Unit.PIECE, min_stock=Decimal("200"))
        drug = Product.objects.create(name="Enrofloksatsin 10% (demo)", category=ProductCategory.VET_DRUG,
                                      unit=Unit.LITRE, min_stock=Decimal("2"))
        for product, quantity, lot_no, days in ((vaccine, "1000", "ND-2611", 10), (vaccine, "2000", "ND-2702", 120),
                                                (drug, "20", "EN-01", 200)):
            warehouse_services.record_stock_transaction(
                actor=self.warehouse, product_id=product.pk, transaction_type="IN", quantity=Decimal(quantity),
                document_no=f"DEMO-{lot_no}", lot_no=lot_no, expiry_date=self.today + timedelta(days=days),
                note=DEMO_NOTE,
            )
        feed = Product.objects.filter(category=ProductCategory.FEED, unit=Unit.KG, is_active=True).order_by("id").first()

        def task(household: Household, quantity: str):
            return logistics.create_task(
                actor=self.admin, household=household,
                items=[logistics.ItemInput(product=feed, quantity=Decimal(quantity))],
                planned_date=self.today + timedelta(days=1), note=DEMO_NOTE,
            )

        if feed is not None and feed.balance >= 600:
            # Orders in every status: NEW, APPROVED (task created), REJECTED.
            orders.create_order(actor=households[0].owner, kind="PRODUCT_ORDER", household=households[0],
                                items=[OrderItemInput(product=feed, quantity=Decimal("100"))], note=DEMO_NOTE)
            approved = orders.create_order(actor=households[1].owner, kind="PRODUCT_ORDER", household=households[1],
                                           items=[OrderItemInput(product=feed, quantity=Decimal("80"))],
                                           desired_date=self.today + timedelta(days=2))
            orders.approve_order(actor=self.admin, order_id=approved.pk,
                                 items=[{"id": item.pk, "approved_qty": item.requested_qty}
                                        for item in approved.items.all()],
                                 decision_note="Demo tasdiq", create_task=True, task_data={})
            rejected = orders.create_order(actor=households[2].owner, kind="SUPPLY_REQUEST", household=households[2],
                                           items=[OrderItemInput(product=feed, quantity=Decimal("50"))])
            orders.reject_order(actor=self.admin, order_id=rejected.pk, reason="Demo: ombor rejasi bo‘yicha emas")
            # Tasks NEW → ASSIGNED → ACCEPTED → IN_TRANSIT → CONFIRMED.
            assigned = task(households[3], "60")
            logistics.assign(actor=self.admin, task_id=assigned.pk, vehicle=vehicle.pk, driver=driver.pk)
            in_transit = task(households[4], "70")
            logistics.assign(actor=self.admin, task_id=in_transit.pk, vehicle=vehicle.pk, driver=driver.pk)
            logistics.accept(actor=logist, task_id=in_transit.pk)
            logistics.start(actor=logist, task_id=in_transit.pk)
            done = task(households[5], "50")
            logistics.assign(actor=self.admin, task_id=done.pk, vehicle=vehicle.pk, driver=driver.pk)
            logistics.accept(actor=logist, task_id=done.pk)
            logistics.start(actor=logist, task_id=done.pk)
            proof = files.upload(
                actor=logist,
                file=SimpleUploadedFile("dalil.png", b"\x89PNG\r\n\x1a\n" + b"\x00" * 64),
                purpose=AttachmentPurpose.DELIVERY_PROOF,
            )
            logistics.deliver(actor=logist, task_id=done.pk, file_ids=[proof.pk], recipient_name="Xonadon egasi")
            logistics.confirm(actor=done.household.owner, task_id=done.pk, received_ok=True)

        # Veterinary: visits (today, done), vaccinations (upcoming, overdue, done), a disease with treatment.
        vet.plan_visit(actor=self.admin, data={"household": households[0], "planned_date": self.today,
                                               "purpose": "Rejali ko‘rik (demo)", "vet": vet_user})
        visit = vet.plan_visit(actor=vet_user, data={"household": households[1], "planned_date": self.today,
                                                     "purpose": "Shikoyat bo‘yicha ko‘rik (demo)"})
        vet.complete_visit(actor=vet_user, pk=visit.pk, data={"health_status": "FAIR", "birds_checked": 120,
                                                               "findings": "Yo‘tal belgilari",
                                                               "conclusion": "Davolash buyurildi"})
        vet.plan_vaccinations(actor=vet_user, data={"household": households[2], "product": vaccine,
                                                    "planned_date": self.today + timedelta(days=2),
                                                    "dose": "0.5 ml / bosh", "birds_planned": 300})
        vet.plan_vaccinations(actor=vet_user, data={"household": households[3], "product": vaccine,
                                                    "planned_date": self.today - timedelta(days=7),
                                                    "dose": "0.5 ml / bosh", "birds_planned": 250})
        done_vaccination = vet.plan_vaccinations(actor=vet_user, data={
            "household": households[4], "product": vaccine, "planned_date": self.today - timedelta(days=1),
            "dose": "0.5 ml / bosh", "birds_planned": 200})[0]
        vet.complete_vaccination(actor=vet_user, pk=done_vaccination.pk, data={
            "done_date": self.today, "birds_vaccinated": 200, "result": "Muvaffaqiyatli", "quantity_used": Decimal("200")})
        disease = vet.register_disease(actor=vet_user, data={"household": households[1], "visit": visit,
                                                             "disease_name": "Mikoplazmoz (demo)",
                                                             "diagnosed_date": self.today, "cases": 12,
                                                             "symptoms": "Yo‘tal, burun oqishi"})
        vet.prescribe_treatment(actor=vet_user, data={"household": households[1], "disease": disease, "product": drug,
                                                      "start_date": self.today,
                                                      "end_date": self.today + timedelta(days=5),
                                                      "dosage": "1 ml / 1 l suv", "quantity_used": Decimal("2"),
                                                      "instructions": "5 kun ichimlik suvi bilan"})
        vet.create_request(actor=households[5].owner, text="Tovuqlar yem yemayapti, ko‘rib bering (demo).",
                           file_ids=[])

    def _staff(self, username: str, first: str, last: str, phone: str, role: str, credentials: list) -> User:
        password = generate_temp_password(12)
        user = User(
            username=username, first_name=first, last_name=last, phone=phone, role=role,
            must_change_password=False, is_staff=role == Role.ADMIN, is_superuser=role == Role.ADMIN,
        )
        user.set_password(password)
        user.save()
        credentials.append((role, username, password))
        return user

    def _batch(self, kind: str, breed: str, quantity: int, days_ago: int, supplier: str, weight: str) -> PoultryBatch:
        batch = farm_services.create_batch(
            actor=self.admin if kind != PoultryType.BROILER else self.warehouse,
            poultry_type=kind, breed=breed, initial_quantity=quantity, age_days=1 if kind == PoultryType.BROILER else 21,
            supplier=supplier, arrived_at=self.today - timedelta(days=days_ago), initial_weight_g=Decimal(weight),
        )
        PoultryBatch.objects.filter(pk=batch.pk).update(created_at=local_dt(batch.arrived_at, 9))
        return batch

    def _seed_batches(self) -> dict[str, PoultryBatch]:
        return {
            "main_a": self._batch(PoultryType.BROILER, "Ross 308", 4200, HISTORY_DAYS + 2,
                                  "Toshkent parranda inkubatori (demo)", "42"),
            "main_b": self._batch(PoultryType.BROILER, "Cobb 500", 1500, 41, "Andijon broyler markazi (demo)", "41"),
            "broiler_a": self._batch(PoultryType.BROILER, "Ross 308", 2600, 55, "Samarqand broyler markazi (demo)", "42"),
            "broiler_b": self._batch(PoultryType.BROILER, "Cobb 500", 1200, 21, "Samarqand broyler markazi (demo)", "41"),
            "breeder": self._batch(PoultryType.BREEDER, "Ross 308 PS", 400, 48, "Toshkent parranda inkubatori (demo)",
                                   "380"),
        }

    def _move(self, batch: PoultryBatch, household: Household | None, kind: str, quantity: int, day: date,
              note: str = "") -> None:
        movement = farm_services.record_movement(
            actor=self.warehouse, batch=batch, household=household, movement_type=kind,
            quantity=quantity, on_date=day, note=note,
        )
        PoultryMovement.objects.filter(pk=movement.pk).update(created_at=local_dt(day, 11, self.rng.randint(0, 59)))

    def _seed_flock(self, households: list[tuple[HouseholdSpec, Household]], batches: dict[str, PoultryBatch]) -> None:
        rng = self.rng
        main_a, main_b = batches["main_a"], batches["main_b"]
        broiler_a, broiler_b = batches["broiler_a"], batches["broiler_b"]
        self._move(main_a, None, MovementType.DEATH, 18, main_a.arrived_at + timedelta(days=1), "Tashishdan keyin")
        self._move(broiler_a, None, MovementType.DEATH, 11, broiler_a.arrived_at + timedelta(days=2))
        self._move(batches["breeder"], None, MovementType.OUT, 40, batches["breeder"].arrived_at + timedelta(days=10),
                   "Naslchilik xo‘jaligiga sotildi")

        # Who gets what and when (the old main batch first, the later households from batch B).
        start_day: dict[int, date] = {}
        for index, (spec, household) in enumerate(households):
            first_day = self.start + timedelta(days=index % 5)
            if spec.main:
                source = main_a if index < 8 else main_b
                day = max(first_day, source.arrived_at + timedelta(days=1))
                self._move(source, household, MovementType.DISTRIBUTE, spec.main, day)
                first_day = day
            if spec.extra:
                source = broiler_a if index % 2 == 0 else broiler_b
                day = max(first_day, source.arrived_at + timedelta(days=2))
                self._move(source, household, MovementType.DISTRIBUTE, spec.extra, day)
                first_day = min(first_day, day) if spec.main else day
            start_day[household.pk] = first_day
        # Parent stock (BREEDER) for one household, so both poultry types appear in the statistics.
        breeder_day = max(start_day[households[1][1].pk], batches["breeder"].arrived_at + timedelta(days=12))
        self._move(batches["breeder"], households[1][1], MovementType.DISTRIBUTE, 120, breeder_day)
        # A few corrections: returns and a sale of grown broilers.
        self._move(main_a, households[3][1], MovementType.RETURN, 30, self.today - timedelta(days=35), "Ortiqcha")
        self._move(broiler_a, households[2][1], MovementType.OUT, 250, self.today - timedelta(days=6),
                   "Bozorga sotildi")
        self._move(broiler_a, households[0][1], MovementType.OUT, 120, self.today - timedelta(days=4),
                   "Bozorga sotildi")

        inactive_from = self.today - timedelta(days=INACTIVE_FOR_DAYS)
        for index, (spec, household) in enumerate(households):
            day = start_day[household.pk] + timedelta(days=1)
            while day <= self.today:
                skip = rng.random() < spec.miss_rate or (spec.username == INACTIVE_USERNAME and day >= inactive_from)
                if day == self.today and index in (4, 7):
                    skip = True  # some households have not reported yet today
                if not skip:
                    self._report(spec, household, day)
                day += timedelta(days=1)

    def _report(self, spec: HouseholdSpec, household: Household, day: date) -> None:
        rng = self.rng
        rows = selectors.household_batches(household.pk)
        broilers = sum(row["count"] for row in rows if row["poultry_type"] == PoultryType.BROILER)
        breeders = sum(row["count"] for row in rows if row["poultry_type"] == PoultryType.BREEDER)
        birds = broilers + breeders
        if birds <= 0:
            return
        deaths = 0
        if rng.random() < spec.death_rate:
            deaths = min(birds, rng.choice((1, 1, 1, 2, 2, 3)) + (3 if spec.death_rate > 0.2 else 0))
        feed = broilers * rng.uniform(0.145, 0.175) + breeders * rng.uniform(0.150, 0.165)
        # Meat output (kg) follows the household's feed conversion; ~1 day in 10 has no meat figure.
        meat = None
        if rng.random() >= 0.1:
            meat = Decimal(feed / max(1.3, rng.gauss(spec.fcr, 0.08))).quantize(Decimal("0.1"))
        weight = None
        if rng.random() < 0.7:  # the flock is weighed on most days
            weight = Decimal(min(2900, 400 + (day - self.start).days * 42 + rng.randint(-25, 25)))
        report = farm_services.create_daily_report(
            actor=household.owner, household=household, on_date=day,
            feed_kg=Decimal(feed).quantize(Decimal("0.1")), deaths=deaths,
            meat_kg=meat, avg_weight_g=weight, note="",
        )
        when = local_dt(day, 19, rng.randint(0, 59))
        DailyReport.objects.filter(pk=report.pk).update(created_at=when)
        if report.death_movement_id:
            PoultryMovement.objects.filter(pk=report.death_movement_id).update(created_at=when)

    def _seed_warehouse(self, households: list[Household]) -> None:
        rng = self.rng

        def product(name: str, category: str, unit: str, min_stock: str) -> Product:
            item = Product.objects.create(name=name, category=category, unit=unit, min_stock=Decimal(min_stock))
            Product.objects.filter(pk=item.pk).update(created_at=local_dt(self.start - timedelta(days=5), 9))
            return item

        starter = product("Yem PK-1 (start)", ProductCategory.FEED, Unit.KG, "800")
        grower_feed = product("Yem PK-5 (o‘sish)", ProductCategory.FEED, Unit.KG, "1500")
        broiler_feed = product("Yem PK-6 (broyler)", ProductCategory.FEED, Unit.KG, "1000")
        sacks = product("Premiks (qop)", ProductCategory.FEED, Unit.SACK, "10")
        vaccine = product("Nyukasl vaksinasi (La Sota)", ProductCategory.VACCINE, Unit.PIECE, "500")
        drug = product("Enrofloksatsin 10%", ProductCategory.VET_DRUG, Unit.LITRE, "5")

        events: list[tuple[datetime, Product, str, Decimal, Household | None, str]] = []
        counter = 0

        def add(day: date, hour: int, item: Product, kind: str, quantity, household: Household | None = None):
            nonlocal counter
            counter += 1
            prefix = "KR" if kind == "IN" else "CH"
            events.append((local_dt(day, hour, rng.randint(0, 59)), item, kind, Decimal(str(quantity)), household,
                           f"{prefix}-{counter:04d}"))

        for offset in range(0, HISTORY_DAYS + 1, 10):  # deliveries every 10 days
            day = self.start + timedelta(days=offset)
            add(day, 8, grower_feed, "IN", rng.choice((4500, 5000, 5500)))
            add(day, 8, broiler_feed, "IN", rng.choice((2000, 2500, 3000)))
            if offset % 20 == 0:
                add(day, 8, starter, "IN", 3000)
                add(day, 9, sacks, "IN", 40)
        add(self.start, 9, vaccine, "IN", 3000)
        add(self.start, 9, drug, "IN", 30)
        for offset in range(2, HISTORY_DAYS + 1, 7):  # weekly hand-outs to the households
            day = self.start + timedelta(days=offset)
            for index, household in enumerate(households[:-1] if offset > HISTORY_DAYS - 12 else households):
                add(day, 14, grower_feed if index % 3 != 2 else broiler_feed, "OUT", rng.randint(35, 70) * 10, household)
                if index % 4 == 0:
                    add(day, 15, sacks, "OUT", 1, household)
            add(day, 16, starter, "OUT", rng.randint(20, 40) * 10)
        for day_offset in (5, 26, 47):
            for household in households[:8]:
                add(self.start + timedelta(days=day_offset), 10, vaccine, "OUT", rng.randint(40, 70), household)
        add(self.today - timedelta(days=15), 11, drug, "OUT", 18, households[5])
        add(self.today - timedelta(days=3), 11, drug, "OUT", 9, households[2])

        for when, item, kind, quantity, household, document_no in sorted(events, key=lambda event: event[0]):
            if kind == "OUT":
                balance = Product.objects.values_list("balance", flat=True).get(pk=item.pk)
                quantity = min(quantity, balance)
                if quantity <= 0:
                    continue
            row = warehouse_services.record_stock_transaction(
                actor=self.warehouse, product_id=item.pk, transaction_type=kind, quantity=quantity,
                document_no=document_no, household=household, note=DEMO_NOTE,
            )
            StockTransaction.objects.filter(pk=row.pk).update(created_at=when)

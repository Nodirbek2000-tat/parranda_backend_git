from __future__ import annotations

from decimal import Decimal

from django.conf import settings
from django.db import models
from django.db.models import Q

from common.softdelete import SoftDeleteManager


class Household(models.Model):
    """A household (xonadon) that receives poultry. Exactly one OWNER user per household."""

    owner = models.OneToOneField(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="household",
        verbose_name="Egasi",
    )
    name = models.CharField("Nomi", max_length=200)
    phone = models.CharField("Telefon", max_length=32)
    region = models.CharField("Viloyat", max_length=120)
    district = models.CharField("Tuman", max_length=120)
    mfy = models.CharField("MFY", max_length=120, blank=True)
    address = models.CharField("Manzil", max_length=255, blank=True)
    latitude = models.DecimalField("Kenglik", max_digits=9, decimal_places=6, null=True, blank=True)
    longitude = models.DecimalField("Uzunlik", max_digits=9, decimal_places=6, null=True, blank=True)
    is_active = models.BooleanField("Faol", default=True)
    contract_info = models.TextField("Shartnoma ma’lumoti", blank=True)
    # TZ §18 "Demo ma’lumotlarni real ma’lumotlardan ajrat": seed_demo / test households are marked and can be
    # filtered out (households list ``?is_demo=``, statistics ``?exclude_demo=1``).
    is_demo = models.BooleanField("Demo / sinov ma’lumoti", default=False)
    created_at = models.DateTimeField("Yaratilgan", auto_now_add=True)
    updated_at = models.DateTimeField("Yangilangan", auto_now=True)
    #: Soft delete (``farm.services.delete_household``): hidden by ``objects``, kept for history.
    deleted_at = models.DateTimeField("O‘chirilgan vaqt", null=True, blank=True, editable=False)

    objects = SoftDeleteManager()
    all_objects = models.Manager()

    class Meta:
        verbose_name = "Xonadon"
        verbose_name_plural = "Xonadonlar"
        ordering = ("id",)
        indexes = [models.Index(fields=["is_active"]), models.Index(fields=["region"]), models.Index(fields=["is_demo"])]
        constraints = [
            models.CheckConstraint(
                condition=Q(latitude__isnull=True) | Q(latitude__gte=-90, latitude__lte=90),
                name="household_latitude_range",
            ),
            models.CheckConstraint(
                condition=Q(longitude__isnull=True) | Q(longitude__gte=-180, longitude__lte=180),
                name="household_longitude_range",
            ),
        ]

    @property
    def code(self) -> str:
        return f"XN-{self.pk:04d}"

    def __str__(self) -> str:
        return f"{self.name} ({self.code})"


class PoultryType(models.TextChoices):
    """Meat poultry only (client decision 2026-11-05: the company works only with meat birds)."""

    BROILER = "BROILER", "Go‘sht yo‘nalishi"
    BREEDER = "BREEDER", "Naslli"


class PoultryBatch(models.Model):
    """A batch (partiya) of birds received by the center. Counts are derived from movements."""

    code = models.CharField("Kodi", max_length=20, unique=True, editable=False)
    year_seq = models.PositiveIntegerField("Yil ichidagi tartib raqami", editable=False)
    poultry_type = models.CharField("Parranda turi", max_length=16, choices=PoultryType.choices)
    breed = models.CharField("Zoti", max_length=120)
    initial_quantity = models.PositiveIntegerField("Boshlang‘ich soni")
    age_days = models.PositiveIntegerField("Yoshi (kun)")
    supplier = models.CharField("Yetkazib beruvchi", max_length=200)
    arrived_at = models.DateField("Kelgan sana")
    initial_weight_g = models.DecimalField(
        "Boshlang‘ich vazn (g)", max_digits=8, decimal_places=2, null=True, blank=True
    )
    health_status = models.CharField("Sog‘lig‘i", max_length=120, default="Sog‘lom")
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="+",
        null=True,
        blank=True,
        verbose_name="Kiritgan",
    )
    created_at = models.DateTimeField("Yaratilgan", auto_now_add=True)
    #: Soft delete (``farm.services.delete_batch``): hidden by ``objects`` (and its movements by
    #: ``PoultryMovement.objects``), kept for history.
    deleted_at = models.DateTimeField("O‘chirilgan vaqt", null=True, blank=True, editable=False)

    objects = SoftDeleteManager()
    all_objects = models.Manager()

    class Meta:
        verbose_name = "Parranda partiyasi"
        verbose_name_plural = "Parranda partiyalari"
        ordering = ("-arrived_at", "-id")
        indexes = [models.Index(fields=["arrived_at"]), models.Index(fields=["poultry_type"])]
        constraints = [
            models.CheckConstraint(condition=Q(initial_quantity__gt=0), name="batch_initial_quantity_gt_0"),
            models.CheckConstraint(
                condition=Q(initial_weight_g__isnull=True) | Q(initial_weight_g__gt=0),
                name="batch_initial_weight_gt_0",
            ),
        ]

    def __str__(self) -> str:
        return self.code


class MovementType(models.TextChoices):
    DISTRIBUTE = "DISTRIBUTE", "Xonadonga tarqatish"
    RETURN = "RETURN", "Xonadondan qaytarish"
    DEATH = "DEATH", "Nobud bo‘lish"
    OUT = "OUT", "Sotish / boshqa chiqim"


class VisibleMovementManager(models.Manager):
    """Movements of soft-deleted batches are hidden together with the batch.

    Every bird number (dashboards, statistics, reports, movement lists) is aggregated from
    ``PoultryMovement.objects`` and the batches' ``initial_quantity`` from ``PoultryBatch.objects``, so both sides
    drop a deleted batch together and the totals stay consistent. ``all_objects`` sees every row (integrity).
    """

    def get_queryset(self) -> models.QuerySet:
        return super().get_queryset().filter(batch__deleted_at__isnull=True)


class PoultryMovement(models.Model):
    """Immutable poultry operation. All bird counts are aggregated from these rows."""

    batch = models.ForeignKey(
        PoultryBatch, on_delete=models.PROTECT, related_name="movements", verbose_name="Partiya"
    )
    household = models.ForeignKey(
        Household,
        on_delete=models.PROTECT,
        related_name="movements",
        null=True,
        blank=True,
        verbose_name="Xonadon",
    )
    type = models.CharField("Turi", max_length=16, choices=MovementType.choices)
    quantity = models.PositiveIntegerField("Miqdori")
    date = models.DateField("Sana")
    note = models.CharField("Izoh", max_length=500, blank=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="+",
        null=True,
        blank=True,
        verbose_name="Kiritgan",
    )
    created_at = models.DateTimeField("Yaratilgan", auto_now_add=True)

    objects = VisibleMovementManager()
    all_objects = models.Manager()

    class Meta:
        verbose_name = "Parranda harakati"
        verbose_name_plural = "Parranda harakatlari"
        ordering = ("-date", "-id")
        indexes = [
            models.Index(fields=["batch", "household"]),
            models.Index(fields=["household", "type"]),
            models.Index(fields=["date"]),
            models.Index(fields=["type", "date"]),
            # Stats: per-household / per-batch reconstruction by date (API_CONTRACT_V2 §7.3).
            models.Index(fields=["household", "date"]),
            models.Index(fields=["batch", "date"]),
        ]
        constraints = [
            models.CheckConstraint(condition=Q(quantity__gt=0), name="movement_quantity_gt_0"),
            models.CheckConstraint(
                condition=~Q(type__in=[MovementType.DISTRIBUTE, MovementType.RETURN])
                | Q(household__isnull=False),
                name="movement_household_required_for_transfer",
            ),
        ]

    @property
    def op_id(self) -> str:
        return f"TR-{self.pk:06d}"

    def __str__(self) -> str:
        return f"{self.op_id} {self.get_type_display()} {self.quantity}"


class DailyReport(models.Model):
    """One report per household per day. ``bird_count`` is computed by the server."""

    household = models.ForeignKey(
        Household, on_delete=models.PROTECT, related_name="daily_reports", verbose_name="Xonadon"
    )
    date = models.DateField("Sana")
    bird_count = models.PositiveIntegerField("Parranda soni")
    feed_kg = models.DecimalField("Yem (kg)", max_digits=10, decimal_places=2)
    # Main production output. Optional: a growing flock has days without meat output (null = not reported).
    meat_kg = models.DecimalField("Go‘sht (kg)", max_digits=10, decimal_places=2, null=True, blank=True)
    deaths = models.PositiveIntegerField("Nobud bo‘lgan", default=0)
    avg_weight_g = models.DecimalField(
        "O‘rtacha vazn (g)", max_digits=8, decimal_places=2, null=True, blank=True
    )
    note = models.CharField("Izoh", max_length=500, blank=True)
    death_movement = models.OneToOneField(
        PoultryMovement,
        on_delete=models.PROTECT,
        related_name="daily_report",
        null=True,
        blank=True,
        verbose_name="Nobud bo‘lish operatsiyasi",
    )
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="+",
        null=True,
        blank=True,
        verbose_name="Kiritgan",
    )
    created_at = models.DateTimeField("Yaratilgan", auto_now_add=True)

    class Meta:
        verbose_name = "Kunlik hisobot"
        verbose_name_plural = "Kunlik hisobotlar"
        ordering = ("-date", "-id")
        indexes = [models.Index(fields=["date"])]
        constraints = [
            models.UniqueConstraint(fields=["household", "date"], name="daily_report_household_date_unique"),
            models.CheckConstraint(condition=Q(feed_kg__gte=Decimal("0")), name="daily_report_feed_gte_0"),
            models.CheckConstraint(
                condition=Q(meat_kg__isnull=True) | Q(meat_kg__gte=Decimal("0")),
                name="daily_report_meat_gte_0",
            ),
            models.CheckConstraint(
                condition=Q(avg_weight_g__isnull=True) | Q(avg_weight_g__gte=Decimal("0")),
                name="daily_report_avg_weight_gte_0",
            ),
        ]

    @property
    def op_id(self) -> str:
        return f"KH-{self.pk:06d}"

    def __str__(self) -> str:
        return f"{self.household} — {self.date:%d.%m.%Y}"

"""Logistics (API_CONTRACT_V3 §7): Vehicle, Driver, LogisticsTask, LogisticsTaskItem, LogisticsTaskStatusLog.

Orders plug in through :mod:`logistics.hooks` (order sync) and ``orders/bridge.py`` (task creation / cancel).
The one-active-task-per-order rule is a database constraint.
"""

from __future__ import annotations

from decimal import Decimal

from django.conf import settings
from django.db import models
from django.db.models import Q
from django.db.models.functions import Lower


class TaskStatus(models.TextChoices):
    NEW = "NEW", "Yangi"
    ASSIGNED = "ASSIGNED", "Biriktirildi"
    ACCEPTED = "ACCEPTED", "Ishga olindi"
    IN_TRANSIT = "IN_TRANSIT", "Yo‘lda"
    DELIVERED = "DELIVERED", "Yetkazildi"
    CONFIRMED = "CONFIRMED", "Tasdiqlandi"
    CANCELLED = "CANCELLED", "Bekor qilindi"


#: Statuses shown on the board / counted in ``by_status`` (§7.6).
ACTIVE_STATUSES = (
    TaskStatus.NEW,
    TaskStatus.ASSIGNED,
    TaskStatus.ACCEPTED,
    TaskStatus.IN_TRANSIT,
    TaskStatus.DELIVERED,
)
#: Work in progress of a vehicle / driver (``active_tasks``).
WORKING_STATUSES = (TaskStatus.ASSIGNED, TaskStatus.ACCEPTED, TaskStatus.IN_TRANSIT)
#: ``planned_date < today`` in one of these → ``is_overdue``.
OVERDUE_STATUSES = (TaskStatus.NEW, TaskStatus.ASSIGNED, TaskStatus.ACCEPTED, TaskStatus.IN_TRANSIT)
#: Statuses that require vehicle + driver.
STAFFED_STATUSES = (
    TaskStatus.ASSIGNED,
    TaskStatus.ACCEPTED,
    TaskStatus.IN_TRANSIT,
    TaskStatus.DELIVERED,
    TaskStatus.CONFIRMED,
)
STARTED_STATUSES = (TaskStatus.IN_TRANSIT, TaskStatus.DELIVERED, TaskStatus.CONFIRMED)


class Vehicle(models.Model):
    plate_number = models.CharField("Davlat raqami", max_length=15)
    model = models.CharField("Modeli", max_length=120, blank=True)
    capacity_kg = models.DecimalField("Sig‘imi (kg)", max_digits=10, decimal_places=2, null=True, blank=True)
    is_active = models.BooleanField("Faol", default=True)
    note = models.CharField("Izoh", max_length=500, blank=True)
    created_at = models.DateTimeField("Yaratilgan", auto_now_add=True)

    class Meta:
        verbose_name = "Transport vositasi"
        verbose_name_plural = "Transport vositalari"
        ordering = ("plate_number", "id")
        constraints = [
            models.UniqueConstraint(Lower("plate_number"), name="vehicle_plate_ci_unique"),
            models.CheckConstraint(
                condition=Q(capacity_kg__isnull=True) | Q(capacity_kg__gt=0), name="vehicle_capacity_gt_0"
            ),
        ]

    def __str__(self) -> str:
        return f"{self.plate_number}, {self.model}" if self.model else self.plate_number


class Driver(models.Model):
    full_name = models.CharField("F.I.Sh.", max_length=200)
    phone = models.CharField("Telefon", max_length=32)
    license_no = models.CharField("Guvohnoma raqami", max_length=32, blank=True)
    user = models.OneToOneField(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="driver",
        null=True,
        blank=True,
        verbose_name="Foydalanuvchi",
    )
    is_active = models.BooleanField("Faol", default=True)
    note = models.CharField("Izoh", max_length=500, blank=True)
    created_at = models.DateTimeField("Yaratilgan", auto_now_add=True)

    class Meta:
        verbose_name = "Haydovchi"
        verbose_name_plural = "Haydovchilar"
        ordering = ("full_name", "id")

    def __str__(self) -> str:
        return self.full_name


class LogisticsTask(models.Model):
    code = models.CharField("Kodi", max_length=20, unique=True, null=True, editable=False)
    order = models.ForeignKey(
        "orders.Order",
        on_delete=models.PROTECT,
        related_name="logistics_tasks",
        null=True,
        blank=True,
        editable=False,
        verbose_name="Buyurtma",
    )
    household = models.ForeignKey(
        "farm.Household", on_delete=models.PROTECT, related_name="logistics_tasks", verbose_name="Xonadon"
    )
    # Delivery snapshot taken at creation (§7.3).
    address_text = models.CharField("Manzil", max_length=700, blank=True)
    latitude = models.DecimalField("Kenglik", max_digits=9, decimal_places=6, null=True, blank=True)
    longitude = models.DecimalField("Uzunlik", max_digits=9, decimal_places=6, null=True, blank=True)
    contact_name = models.CharField("Qabul qiluvchi", max_length=200, blank=True)
    contact_phone = models.CharField("Telefon", max_length=32, blank=True)

    status = models.CharField("Holati", max_length=16, choices=TaskStatus.choices, default=TaskStatus.NEW)
    planned_date = models.DateField("Reja sanasi", null=True, blank=True)
    vehicle = models.ForeignKey(
        Vehicle, on_delete=models.PROTECT, related_name="tasks", null=True, blank=True, verbose_name="Transport"
    )
    driver = models.ForeignKey(
        Driver, on_delete=models.PROTECT, related_name="tasks", null=True, blank=True, verbose_name="Haydovchi"
    )
    responsible = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="+",
        null=True,
        blank=True,
        verbose_name="Mas’ul",
    )
    assign_seq = models.PositiveIntegerField("Biriktirish tartibi", default=0, editable=False)
    note = models.CharField("Izoh", max_length=500, blank=True)

    assigned_at = models.DateTimeField("Biriktirilgan", null=True, blank=True)
    accepted_at = models.DateTimeField("Ishga olingan", null=True, blank=True)
    started_at = models.DateTimeField("Yo‘lga chiqqan", null=True, blank=True)
    delivered_at = models.DateTimeField("Yetkazilgan", null=True, blank=True)
    confirmed_at = models.DateTimeField("Tasdiqlangan", null=True, blank=True)
    cancelled_at = models.DateTimeField("Bekor qilingan", null=True, blank=True)

    delivery_note = models.CharField("Yetkazish izohi", max_length=500, blank=True)
    recipient_name = models.CharField("Qabul qilgan shaxs", max_length=200, blank=True)
    received_ok = models.BooleanField("Muammosiz qabul qilindi", null=True, blank=True)
    issue_note = models.CharField("Muammo", max_length=1000, blank=True)
    cancel_reason = models.CharField("Bekor qilish sababi", max_length=500, blank=True)

    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="+",
        null=True,
        blank=True,
        verbose_name="Yaratgan",
    )
    created_at = models.DateTimeField("Yaratilgan", auto_now_add=True)
    updated_at = models.DateTimeField("Yangilangan", auto_now=True)

    class Meta:
        verbose_name = "Logistika topshirig‘i"
        verbose_name_plural = "Logistika topshiriqlari"
        ordering = ("-id",)
        indexes = [
            models.Index(fields=["status", "planned_date"]),
            models.Index(fields=["household", "status"]),
            models.Index(fields=["responsible", "status"]),
        ]
        constraints = [
            models.CheckConstraint(condition=Q(status__in=TaskStatus.values), name="logistics_task_status_valid"),
            models.CheckConstraint(
                condition=~Q(status__in=STAFFED_STATUSES) | Q(vehicle__isnull=False, driver__isnull=False),
                name="logistics_task_staffed_has_vehicle_driver",
            ),
            models.CheckConstraint(
                condition=~Q(status__in=STARTED_STATUSES) | Q(started_at__isnull=False),
                name="logistics_task_started_has_time",
            ),
            models.UniqueConstraint(
                fields=["order"],
                condition=Q(order_id__isnull=False) & ~Q(status=TaskStatus.CANCELLED),
                name="one_active_task_per_order",
            ),
        ]

    def __str__(self) -> str:
        return self.code or f"LG-{self.pk}"

    @staticmethod
    def code_for(pk: int) -> str:
        return f"LG-{pk:06d}"


class LogisticsTaskItem(models.Model):
    task = models.ForeignKey(LogisticsTask, on_delete=models.CASCADE, related_name="items", verbose_name="Topshiriq")
    product = models.ForeignKey(
        "warehouse.Product", on_delete=models.PROTECT, related_name="+", verbose_name="Mahsulot"
    )
    quantity = models.DecimalField("Miqdori", max_digits=12, decimal_places=2)
    order_item = models.ForeignKey(
        "orders.OrderItem",
        on_delete=models.PROTECT,
        related_name="+",
        null=True,
        blank=True,
        editable=False,
        verbose_name="Buyurtma qatori",
    )
    reserved_qty = models.DecimalField(
        "Zaxiralangan", max_digits=12, decimal_places=2, default=Decimal("0"), editable=False
    )
    stock_transaction = models.OneToOneField(
        "warehouse.StockTransaction",
        on_delete=models.PROTECT,
        related_name="+",
        null=True,
        blank=True,
        editable=False,
        verbose_name="Chiqim operatsiyasi",
    )
    return_transaction = models.OneToOneField(
        "warehouse.StockTransaction",
        on_delete=models.PROTECT,
        related_name="+",
        null=True,
        blank=True,
        editable=False,
        verbose_name="Qaytarish operatsiyasi",
    )

    class Meta:
        verbose_name = "Topshiriq mahsuloti"
        verbose_name_plural = "Topshiriq mahsulotlari"
        ordering = ("id",)
        constraints = [
            models.CheckConstraint(condition=Q(quantity__gt=0), name="logistics_item_quantity_gt_0"),
            models.CheckConstraint(condition=Q(reserved_qty__gte=0), name="logistics_item_reserved_gte_0"),
            models.UniqueConstraint(fields=["task", "product"], name="logistics_item_unique_product"),
        ]

    def __str__(self) -> str:
        return f"{self.task} {self.product_id} {self.quantity}"


class StatusSource(models.TextChoices):
    API = "API", "Web"
    BOT = "BOT", "Telegram bot"


class LogisticsTaskStatusLog(models.Model):
    task = models.ForeignKey(
        LogisticsTask, on_delete=models.CASCADE, related_name="history", verbose_name="Topshiriq"
    )
    from_status = models.CharField("Oldingi holat", max_length=16, blank=True)
    to_status = models.CharField("Yangi holat", max_length=16, choices=TaskStatus.choices)
    actor = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+", null=True, blank=True,
        verbose_name="Bajargan",
    )
    actor_name = models.CharField("Bajargan (ism)", max_length=150, blank=True)
    source = models.CharField("Manba", max_length=8, choices=StatusSource.choices, default=StatusSource.API)
    note = models.CharField("Izoh", max_length=1000, blank=True)
    latitude = models.DecimalField("Kenglik", max_digits=9, decimal_places=6, null=True, blank=True)
    longitude = models.DecimalField("Uzunlik", max_digits=9, decimal_places=6, null=True, blank=True)
    created_at = models.DateTimeField("Vaqti", auto_now_add=True)

    class Meta:
        verbose_name = "Topshiriq holati tarixi"
        verbose_name_plural = "Topshiriq holatlari tarixi"
        ordering = ("created_at", "id")
        indexes = [models.Index(fields=["task", "created_at"])]

    def __str__(self) -> str:
        return f"{self.task_id}: {self.from_status} → {self.to_status}"

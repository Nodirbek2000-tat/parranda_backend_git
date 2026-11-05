"""Orders (API_CONTRACT_V3 §6): Order, OrderItem, OrderStatusLog.

Orders are never deleted from the database: ``DELETE /api/orders/{id}`` is a soft delete
(:func:`orders.services.delete_order`, ``deleted_at`` + :class:`~common.softdelete.SoftDeleteManager`). Status changes happen only in :mod:`orders.services` (own transitions) and,
for the delivery part of the life cycle, through :func:`orders.services.sync_order_with_task`, which the
logistics services call under the order row lock.
"""

from __future__ import annotations

from django.conf import settings
from django.db import models
from django.db.models import Q

from common.softdelete import SoftDeleteManager


class OrderKind(models.TextChoices):
    PRODUCT_ORDER = "PRODUCT_ORDER", "Mahsulot buyurtmasi"
    SUPPLY_REQUEST = "SUPPLY_REQUEST", "Yem yoki mahsulot talabi"


class OrderStatus(models.TextChoices):
    NEW = "NEW", "Yangi"
    APPROVED = "APPROVED", "Tasdiqlandi"
    REJECTED = "REJECTED", "Rad etildi"
    IN_DELIVERY = "IN_DELIVERY", "Yetkazilmoqda"
    DELIVERED = "DELIVERED", "Yetkazildi"
    CONFIRMED = "CONFIRMED", "Qabul qilindi"
    CANCELLED = "CANCELLED", "Bekor qilindi"


#: Statuses after which nothing can happen any more.
FINAL_STATUSES = frozenset({OrderStatus.REJECTED, OrderStatus.CONFIRMED, OrderStatus.CANCELLED})
#: “Open” orders of an owner (dashboard ``orders.open``): waiting for a decision or on the way.
OPEN_STATUSES = (OrderStatus.NEW, OrderStatus.APPROVED, OrderStatus.IN_DELIVERY, OrderStatus.DELIVERED)

NOTE_MAX = 500
ITEM_NOTE_MAX = 200
MAX_ITEMS = 20


class Order(models.Model):
    kind = models.CharField("Turi", max_length=16, choices=OrderKind.choices)
    household = models.ForeignKey(
        "farm.Household", on_delete=models.PROTECT, related_name="orders", verbose_name="Xonadon"
    )
    status = models.CharField("Holati", max_length=16, choices=OrderStatus.choices, default=OrderStatus.NEW)
    note = models.CharField("Izoh", max_length=NOTE_MAX, blank=True)
    desired_date = models.DateField("Kerakli sana", null=True, blank=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+", verbose_name="Yaratgan"
    )
    created_at = models.DateTimeField("Yaratilgan", auto_now_add=True)
    decided_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="+",
        null=True,
        blank=True,
        verbose_name="Qaror qilgan",
    )
    decided_at = models.DateTimeField("Qaror vaqti", null=True, blank=True)
    decision_note = models.CharField("Qaror izohi", max_length=NOTE_MAX, blank=True)
    cancelled_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="+",
        null=True,
        blank=True,
        verbose_name="Bekor qilgan",
    )
    cancelled_at = models.DateTimeField("Bekor qilingan", null=True, blank=True)
    cancel_reason = models.CharField("Bekor qilish sababi", max_length=NOTE_MAX, blank=True)
    updated_at = models.DateTimeField("Yangilangan", auto_now=True)
    #: Soft delete (``orders.services.delete_order``): hidden by ``objects``, kept for task / audit history.
    deleted_at = models.DateTimeField("O‘chirilgan vaqt", null=True, blank=True, editable=False)

    objects = SoftDeleteManager()
    all_objects = models.Manager()

    class Meta:
        verbose_name = "Buyurtma"
        verbose_name_plural = "Buyurtmalar"
        ordering = ("-created_at", "-id")
        indexes = [
            models.Index(fields=["status", "created_at"], name="order_status_created_idx"),
            models.Index(fields=["household", "created_at"], name="order_household_created_idx"),
        ]
        constraints = [
            models.CheckConstraint(condition=Q(kind__in=OrderKind.values), name="order_kind_valid"),
            models.CheckConstraint(condition=Q(status__in=OrderStatus.values), name="order_status_valid"),
            models.CheckConstraint(
                condition=~Q(status=OrderStatus.CANCELLED) | Q(cancelled_at__isnull=False),
                name="order_cancelled_has_time",
            ),
            models.CheckConstraint(
                condition=Q(status__in=[OrderStatus.NEW, OrderStatus.CANCELLED]) | Q(decided_at__isnull=False),
                name="order_decided_has_time",
            ),
        ]

    @property
    def code(self) -> str:
        return f"BY-{self.pk:06d}" if self.pk else ""

    def __str__(self) -> str:
        return f"{self.code} {self.get_kind_display()}"


class OrderItem(models.Model):
    # CASCADE is never exercised: orders are only soft-deleted (``deleted_at``), never removed.
    order = models.ForeignKey(Order, on_delete=models.CASCADE, related_name="items", verbose_name="Buyurtma")
    product = models.ForeignKey(
        "warehouse.Product", on_delete=models.PROTECT, related_name="order_items", verbose_name="Mahsulot"
    )
    requested_qty = models.DecimalField("So‘ralgan miqdor", max_digits=12, decimal_places=2)
    approved_qty = models.DecimalField("Tasdiqlangan miqdor", max_digits=12, decimal_places=2, null=True, blank=True)
    note = models.CharField("Izoh", max_length=ITEM_NOTE_MAX, blank=True)

    class Meta:
        verbose_name = "Buyurtma mahsuloti"
        verbose_name_plural = "Buyurtma mahsulotlari"
        ordering = ("id",)
        constraints = [
            models.UniqueConstraint(fields=["order", "product"], name="order_item_unique_product"),
            models.CheckConstraint(condition=Q(requested_qty__gt=0), name="order_item_requested_gt_0"),
            models.CheckConstraint(
                condition=Q(approved_qty__isnull=True) | Q(approved_qty__gte=0), name="order_item_approved_gte_0"
            ),
        ]

    def __str__(self) -> str:
        return f"{self.product} — {self.requested_qty}"


class OrderStatusLog(models.Model):
    order = models.ForeignKey(Order, on_delete=models.CASCADE, related_name="status_logs", verbose_name="Buyurtma")
    from_status = models.CharField("Oldingi holat", max_length=16, blank=True)
    to_status = models.CharField("Yangi holat", max_length=16, choices=OrderStatus.choices)
    actor = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        related_name="+",
        null=True,
        blank=True,
        verbose_name="Bajaruvchi",
    )
    actor_name = models.CharField("Bajaruvchi (nomi)", max_length=200, blank=True)
    note = models.CharField("Izoh", max_length=NOTE_MAX, blank=True)
    created_at = models.DateTimeField("Vaqt", auto_now_add=True)

    class Meta:
        verbose_name = "Buyurtma holati tarixi"
        verbose_name_plural = "Buyurtma holatlari tarixi"
        ordering = ("created_at", "id")
        indexes = [models.Index(fields=["order", "created_at"], name="order_log_order_created_idx")]

    def __str__(self) -> str:
        return f"{self.order_id}: {self.from_status or '—'} → {self.to_status}"

from __future__ import annotations

from decimal import Decimal

from django.conf import settings
from django.db import models
from django.db.models import F, Q
from django.db.models.functions import Lower


class ProductCategory(models.TextChoices):
    FEED = "FEED", "Yem"
    VET_DRUG = "VET_DRUG", "Veterinariya preparati"
    VACCINE = "VACCINE", "Vaksina"
    EQUIPMENT = "EQUIPMENT", "Jihoz"
    OTHER = "OTHER", "Boshqa"


class Unit(models.TextChoices):
    KG = "kg", "kg"
    PIECE = "dona", "dona"
    LITRE = "litr", "litr"
    SACK = "qop", "qop"


#: Largest value a ``max_digits=12, decimal_places=2`` column (balance, quantity) can hold.
MAX_AMOUNT = Decimal("9999999999.99")

#: Units that can only be counted in whole numbers.
WHOLE_UNITS = frozenset({Unit.PIECE, Unit.SACK})


class Product(models.Model):
    """Warehouse item. ``balance`` is changed only by :class:`StockTransaction` rows."""

    name = models.CharField("Nomi", max_length=200)
    category = models.CharField("Toifa", max_length=16, choices=ProductCategory.choices)
    unit = models.CharField("O‘lchov birligi", max_length=8, choices=Unit.choices)
    min_stock = models.DecimalField("Minimal zaxira", max_digits=12, decimal_places=2, default=Decimal("0"))
    balance = models.DecimalField(
        "Qoldiq", max_digits=12, decimal_places=2, default=Decimal("0"), editable=False
    )
    #: Quantity promised to active logistics tasks (API_CONTRACT_V3 §5.1); changed only by
    #: ``warehouse.services.reserve/release/post_task_out`` under the product row lock.
    reserved = models.DecimalField(
        "Zaxiralangan", max_digits=12, decimal_places=2, default=Decimal("0"), editable=False
    )
    is_active = models.BooleanField("Faol", default=True)
    created_at = models.DateTimeField("Yaratilgan", auto_now_add=True)

    class Meta:
        verbose_name = "Mahsulot"
        verbose_name_plural = "Mahsulotlar"
        ordering = ("name", "id")
        indexes = [models.Index(fields=["category"]), models.Index(fields=["is_active"])]
        constraints = [
            models.UniqueConstraint(Lower("name"), name="product_name_ci_unique"),
            models.CheckConstraint(condition=Q(min_stock__gte=0), name="product_min_stock_gte_0"),
            models.CheckConstraint(condition=Q(balance__gte=0), name="product_balance_gte_0"),
            models.CheckConstraint(condition=Q(reserved__gte=0), name="product_reserved_gte_0"),
            models.CheckConstraint(condition=Q(reserved__lte=F("balance")), name="product_reserved_lte_balance"),
        ]

    @property
    def is_low(self) -> bool:
        return self.balance <= self.min_stock

    @property
    def available(self) -> Decimal:
        """Stock that may still be spent: ``balance − reserved``."""
        return self.balance - self.reserved

    def __str__(self) -> str:
        return self.name


class StockTransactionType(models.TextChoices):
    IN = "IN", "Kirim"
    OUT = "OUT", "Chiqim"
    # V3 §5.3: created only by inventory approval (never via POST /api/stock-transactions).
    ADJUST_IN = "ADJUST_IN", "Inventarizatsiya: ortiqcha"
    ADJUST_OUT = "ADJUST_OUT", "Inventarizatsiya: kamomad"


#: Types that add to / take from the balance (V3 §5.2: every outgoing quantity consumes lots).
INCOMING_TYPES = frozenset({StockTransactionType.IN, StockTransactionType.ADJUST_IN})
OUTGOING_TYPES = frozenset({StockTransactionType.OUT, StockTransactionType.ADJUST_OUT})
ADJUST_TYPES = frozenset({StockTransactionType.ADJUST_IN, StockTransactionType.ADJUST_OUT})


def signed_quantity(tx_type: str, quantity: Decimal) -> Decimal:
    """``+quantity`` for IN / ADJUST_IN, ``-quantity`` for OUT / ADJUST_OUT."""
    return -quantity if tx_type in OUTGOING_TYPES else quantity


class StockTransaction(models.Model):
    """Immutable stock operation; ``balance_after`` is the product balance right after it."""

    product = models.ForeignKey(
        Product, on_delete=models.PROTECT, related_name="transactions", verbose_name="Mahsulot"
    )
    type = models.CharField("Turi", max_length=16, choices=StockTransactionType.choices)
    quantity = models.DecimalField("Miqdori", max_digits=12, decimal_places=2)
    balance_after = models.DecimalField("Qoldiq (keyin)", max_digits=12, decimal_places=2)
    household = models.ForeignKey(
        "farm.Household",
        on_delete=models.PROTECT,
        related_name="stock_transactions",
        null=True,
        blank=True,
        verbose_name="Xonadon",
    )
    document_no = models.CharField("Hujjat raqami", max_length=64)
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
    #: Business object that caused the operation (V3 §5.4): ``logistics_task``, ``vaccination``,
    #: ``treatment``, ``inventory``; empty for manual operations.
    reference_type = models.CharField("Manba turi", max_length=32, blank=True, default="", editable=False)
    reference_id = models.BigIntegerField("Manba ID", null=True, blank=True, editable=False)

    class Meta:
        verbose_name = "Ombor operatsiyasi"
        verbose_name_plural = "Ombor operatsiyalari"
        ordering = ("-created_at", "-id")
        indexes = [
            models.Index(fields=["product", "created_at"]),
            models.Index(fields=["household", "type"]),
            models.Index(fields=["created_at"]),
            # Stats: period filters by type (API_CONTRACT_V2 §7.3).
            models.Index(fields=["type", "created_at"]),
        ]
        constraints = [
            models.CheckConstraint(condition=Q(quantity__gt=0), name="stock_tx_quantity_gt_0"),
            models.CheckConstraint(condition=Q(balance_after__gte=0), name="stock_tx_balance_after_gte_0"),
            models.CheckConstraint(
                condition=Q(type=StockTransactionType.OUT) | Q(household__isnull=True),
                name="stock_tx_household_only_for_out",
            ),
            models.CheckConstraint(condition=~Q(document_no=""), name="stock_tx_document_no_not_empty"),
            models.CheckConstraint(
                condition=Q(reference_type="", reference_id__isnull=True)
                | (~Q(reference_type="") & Q(reference_id__isnull=False)),
                name="stock_tx_reference_pair",
            ),
        ]

    @property
    def op_id(self) -> str:
        return f"OM-{self.pk:06d}"

    def __str__(self) -> str:
        return f"{self.op_id} {self.get_type_display()} {self.product} {self.quantity}"


# --- V3 §5.2 lots and expiry ------------------------------------------------------------------


class StockLot(models.Model):
    """One received lot (every IN creates exactly one). ``remaining_qty`` changes only in warehouse services."""

    product = models.ForeignKey(Product, on_delete=models.PROTECT, related_name="lots", verbose_name="Mahsulot")
    lot_no = models.CharField("Partiya raqami", max_length=64, blank=True)
    expiry_date = models.DateField("Yaroqlilik muddati", null=True, blank=True)
    received_qty = models.DecimalField("Qabul qilingan", max_digits=12, decimal_places=2)
    remaining_qty = models.DecimalField("Qoldiq", max_digits=12, decimal_places=2)
    in_transaction = models.OneToOneField(
        StockTransaction, on_delete=models.PROTECT, related_name="lot", verbose_name="Kirim operatsiyasi"
    )
    created_at = models.DateTimeField("Yaratilgan", auto_now_add=True)

    class Meta:
        verbose_name = "Mahsulot partiyasi"
        verbose_name_plural = "Mahsulot partiyalari"
        ordering = (models.F("expiry_date").asc(nulls_last=True), "id")
        indexes = [
            models.Index(fields=["product", "expiry_date"]),
            models.Index(fields=["expiry_date"], condition=Q(remaining_qty__gt=0), name="stock_lot_open_expiry"),
        ]
        constraints = [
            models.CheckConstraint(condition=Q(received_qty__gt=0), name="stock_lot_received_gt_0"),
            models.CheckConstraint(condition=Q(remaining_qty__gte=0), name="stock_lot_remaining_gte_0"),
            models.CheckConstraint(
                condition=Q(remaining_qty__lte=models.F("received_qty")), name="stock_lot_remaining_lte_received"
            ),
        ]

    def __str__(self) -> str:
        return f"{self.product} {self.lot_no or f'#{self.pk}'}"


class StockLotAllocation(models.Model):
    """How much of a lot an outgoing operation (OUT / ADJUST_OUT) consumed."""

    transaction = models.ForeignKey(
        StockTransaction, on_delete=models.PROTECT, related_name="lot_allocations", verbose_name="Operatsiya"
    )
    lot = models.ForeignKey(StockLot, on_delete=models.PROTECT, related_name="allocations", verbose_name="Partiya")
    quantity = models.DecimalField("Miqdori", max_digits=12, decimal_places=2)

    class Meta:
        verbose_name = "Partiyadan sarf"
        verbose_name_plural = "Partiyalardan sarf"
        ordering = ("id",)
        constraints = [
            models.CheckConstraint(condition=Q(quantity__gt=0), name="stock_lot_alloc_quantity_gt_0"),
            models.UniqueConstraint(fields=["transaction", "lot"], name="stock_lot_alloc_unique"),
        ]


# --- V3 §5.3 inventory count ------------------------------------------------------------------


class InventoryCountStatus(models.TextChoices):
    DRAFT = "DRAFT", "Qoralama"
    APPROVED = "APPROVED", "Tasdiqlangan"
    CANCELLED = "CANCELLED", "Bekor qilingan"


class InventoryCount(models.Model):
    status = models.CharField(
        "Holati", max_length=16, choices=InventoryCountStatus.choices, default=InventoryCountStatus.DRAFT
    )
    note = models.CharField("Izoh", max_length=500, blank=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+", null=True, verbose_name="Yaratgan"
    )
    created_at = models.DateTimeField("Yaratilgan", auto_now_add=True)
    approved_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+", null=True, blank=True,
        verbose_name="Tasdiqlagan",
    )
    approved_at = models.DateTimeField("Tasdiqlangan", null=True, blank=True)
    cancelled_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+", null=True, blank=True,
        verbose_name="Bekor qilgan",
    )
    cancelled_at = models.DateTimeField("Bekor qilingan", null=True, blank=True)
    cancel_reason = models.CharField("Bekor qilish sababi", max_length=500, blank=True)

    class Meta:
        verbose_name = "Inventarizatsiya"
        verbose_name_plural = "Inventarizatsiyalar"
        ordering = ("-created_at", "-id")
        indexes = [models.Index(fields=["status", "created_at"])]
        constraints = [
            models.CheckConstraint(
                condition=~Q(status=InventoryCountStatus.APPROVED) | Q(approved_at__isnull=False),
                name="inventory_approved_has_time",
            ),
            models.CheckConstraint(
                condition=~Q(status=InventoryCountStatus.CANCELLED) | Q(cancelled_at__isnull=False),
                name="inventory_cancelled_has_time",
            ),
        ]

    @property
    def code(self) -> str:
        return f"INV-{self.pk:06d}" if self.pk else ""

    def __str__(self) -> str:
        return self.code


class InventoryCountLine(models.Model):
    count = models.ForeignKey(InventoryCount, on_delete=models.CASCADE, related_name="lines", verbose_name="Inventarizatsiya")
    product = models.ForeignKey(Product, on_delete=models.PROTECT, related_name="+", verbose_name="Mahsulot")
    expected_qty = models.DecimalField("Kutilgan (yaratilganda)", max_digits=12, decimal_places=2)
    counted_qty = models.DecimalField("Sanalgan", max_digits=12, decimal_places=2, null=True, blank=True)
    balance_at_approval = models.DecimalField(
        "Tasdiqlashdagi qoldiq", max_digits=12, decimal_places=2, null=True, blank=True
    )
    difference = models.DecimalField("Farq", max_digits=13, decimal_places=2, null=True, blank=True)
    adjustment = models.OneToOneField(
        StockTransaction, on_delete=models.PROTECT, related_name="inventory_line", null=True, blank=True,
        verbose_name="Tuzatish operatsiyasi",
    )
    counted_at = models.DateTimeField("Sanalgan vaqt", null=True, blank=True)

    class Meta:
        verbose_name = "Inventarizatsiya qatori"
        verbose_name_plural = "Inventarizatsiya qatorlari"
        ordering = ("id",)
        constraints = [
            models.UniqueConstraint(fields=["count", "product"], name="inventory_line_unique_product"),
            models.CheckConstraint(
                condition=Q(counted_qty__isnull=True) | Q(counted_qty__gte=0), name="inventory_line_counted_gte_0"
            ),
        ]


TASK_REFERENCE = "logistics_task"


def returned_task_out() -> Q:
    """``Q`` matching an OUT of a logistics task whose goods came back (cancel in transit → return ``IN`` of the
    same task and product). Such an OUT is not "issued to the household": household-level consumption (stats
    top consumers, the feed report, the owner's own history) excludes it with ``.exclude(returned_task_out())``.
    Product-level ``in_qty``/``out_qty`` keep both legs of the round trip (the goods physically left and came back).
    """
    return Q(reference_type=TASK_REFERENCE) & Q(
        models.Exists(
            StockTransaction.objects.filter(
                type=StockTransactionType.IN,
                reference_type=TASK_REFERENCE,
                reference_id=models.OuterRef("reference_id"),
                product_id=models.OuterRef("product_id"),
            )
        )
    )

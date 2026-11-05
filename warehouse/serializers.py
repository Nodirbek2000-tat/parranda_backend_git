from __future__ import annotations

from core.files import FileIdsField, files_by_object, files_for
from core.serializers import AttachmentSerializer

from datetime import date
from decimal import Decimal
from typing import Any

from django.utils import timezone
from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import extend_schema_field, inline_serializer
from rest_framework import serializers

from accounts.serializers import UserRefSerializer
from common.fields import PrimaryKeyField
from common.ids import fits_db_int
from common.roles import Role, role_of
from farm.models import Household, PoultryType
from farm.serializers import HouseholdShortSerializer

from . import lots as lot_reads
from .models import (
    MAX_AMOUNT,
    NORM_DAY_MAX,
    NORM_DAY_MIN,
    OUTGOING_TYPES,
    WHOLE_UNITS,
    ConstructionOption,
    FeedNorm,
    InventoryCount,
    InventoryCountLine,
    Product,
    ProductCategory,
    StockLot,
    StockTransaction,
    StockTransactionType,
)
from .norms import MSG_OPTION_DUPLICATE
from .readiness import models_ready
from .services import MSG_DUPLICATE_NAME

_UNSET = object()


def lots_ready() -> bool:
    """Lot tables exist (False only on a database that has not run the V3 warehouse migration yet)."""
    from .models import StockLotAllocation

    return models_ready(StockLot, StockLotAllocation)


class ProductSerializer(serializers.ModelSerializer):
    category_display = serializers.CharField(source="get_category_display", read_only=True)
    min_stock = serializers.DecimalField(max_digits=12, decimal_places=2, min_value=0)
    balance = serializers.DecimalField(max_digits=12, decimal_places=2, read_only=True)
    reserved = serializers.DecimalField(max_digits=12, decimal_places=2, read_only=True)
    available = serializers.DecimalField(max_digits=13, decimal_places=2, read_only=True)
    is_low = serializers.BooleanField(read_only=True)
    nearest_expiry = serializers.SerializerMethodField()
    # V4 §5: optional catalogue data.
    price = serializers.DecimalField(max_digits=14, decimal_places=2, min_value=0, required=False, allow_null=True)
    consumption_norm = serializers.DecimalField(
        max_digits=12, decimal_places=3, min_value=0, required=False, allow_null=True
    )
    dosage = serializers.CharField(max_length=200, required=False, allow_blank=True, trim_whitespace=True)
    description = serializers.CharField(max_length=1000, required=False, allow_blank=True, trim_whitespace=True)

    class Meta:
        model = Product
        fields = (
            "id",
            "name",
            "category",
            "category_display",
            "unit",
            "min_stock",
            "balance",
            "reserved",
            "available",
            "is_low",
            "nearest_expiry",
            "is_active",
            "price",
            "consumption_norm",
            "dosage",
            "description",
            "created_at",
        )
        read_only_fields = ("id", "balance", "reserved", "available", "is_low", "nearest_expiry", "created_at")
        extra_kwargs = {"is_active": {"required": False}}

    @extend_schema_field(serializers.DateField(allow_null=True))
    def get_nearest_expiry(self, product: Product) -> str | None:
        """V3 §5.1: min ``expiry_date`` of lots with ``remaining_qty > 0`` (annotated by the list query)."""
        value = getattr(product, "nearest_expiry", _UNSET)
        if value is _UNSET:
            if product.pk is None or not lots_ready():
                return None
            value = (
                StockLot.objects.filter(product_id=product.pk, remaining_qty__gt=0, expiry_date__isnull=False)
                .order_by("expiry_date")
                .values_list("expiry_date", flat=True)
                .first()
            )
        if isinstance(value, str):
            return value
        return value.isoformat() if value is not None else None

    def validate_name(self, value: str) -> str:
        value = value.strip()
        duplicates = Product.objects.filter(name__iexact=value)
        if self.instance is not None:
            duplicates = duplicates.exclude(pk=self.instance.pk)
        if duplicates.exists():
            raise serializers.ValidationError(MSG_DUPLICATE_NAME)
        return value

    def validate_unit(self, value: str) -> str:
        # A fractional balance cannot be re-labelled as a whole-number unit (dona, qop).
        product = self.instance
        if product is not None and value in WHOLE_UNITS and product.balance != product.balance.to_integral_value():
            raise serializers.ValidationError(f"Qoldiq kasr son: “{value}” birligiga o‘tkazib bo‘lmaydi")
        return value

    def save(self, **kwargs):
        raise NotImplementedError("Use warehouse.services.create_product / update_product (audit log).")


class ProductRefSerializer(serializers.ModelSerializer):
    class Meta:
        model = Product
        fields = ("id", "name", "unit")


class ProductShortSerializer(serializers.ModelSerializer):
    """``{"id", "name", "unit", "category"}`` (lots, inventory lines)."""

    class Meta:
        model = Product
        fields = ("id", "name", "unit", "category")


# --- Stock operations (V3 §5.2, §5.4) ------------------------------------------------------------

LOT_REF_SCHEMA = inline_serializer(
    "StockTransactionLot",
    {
        "lot_id": serializers.IntegerField(),
        "lot_no": serializers.CharField(),
        "expiry_date": serializers.DateField(allow_null=True),
        "quantity": serializers.FloatField(),
    },
    many=True,
)
REFERENCE_SCHEMA = inline_serializer(
    "StockTransactionReference",
    {
        "type": serializers.CharField(),
        "id": serializers.IntegerField(),
        "code": serializers.CharField(),
        "path": serializers.CharField(allow_null=True),
    },
    allow_null=True,
)

#: ``reference_type`` → (code prefix, staff path template or None).
REFERENCE_KINDS: dict[str, tuple[str, str | None]] = {
    "logistics_task": ("LG", "/logistics/{id}"),
    "vaccination": ("EM", "/vet?tab=vaccinations"),
    "treatment": ("DV", "/vet?tab=treatments"),
    "inventory": ("INV", "/warehouse/inventory/{id}"),
}


def _lot_entry(lot: StockLot, quantity: Decimal) -> dict[str, Any]:
    return {
        "lot_id": lot.pk,
        "lot_no": lot.lot_no,
        "expiry_date": lot.expiry_date.isoformat() if lot.expiry_date else None,
        "quantity": float(quantity),
    }


def with_lots(qs):
    """Prefetch what :class:`StockTransactionSerializer` needs for ``lots`` (no N+1); no-op before migrate."""
    if not lots_ready():
        return qs
    return qs.prefetch_related("lot_allocations__lot", "lot")


def task_order_ids(rows) -> dict[int, int | None]:
    """``{task_id: order_id}`` of the logistics references in ``rows`` (one query; owner paths)."""
    from .readiness import optional_model

    ids = {row.reference_id for row in rows if row.reference_type == "logistics_task" and row.reference_id}
    model = optional_model("logistics", "LogisticsTask", "order_id") if ids else None
    if model is None:
        return {}
    return dict(model.objects.filter(pk__in=ids).values_list("pk", "order_id"))


class StockTransactionSerializer(serializers.ModelSerializer):
    op_id = serializers.CharField(read_only=True)
    product = ProductRefSerializer(read_only=True)
    type_display = serializers.CharField(source="get_type_display", read_only=True)
    household = HouseholdShortSerializer(read_only=True, allow_null=True)
    created_by = UserRefSerializer(read_only=True, allow_null=True)
    #: ``null`` for owners (see ``to_representation``).
    balance_after = serializers.DecimalField(max_digits=12, decimal_places=2, read_only=True, allow_null=True)
    lots = serializers.SerializerMethodField()
    reference = serializers.SerializerMethodField()
    files = serializers.SerializerMethodField()

    class Meta:
        model = StockTransaction
        fields = (
            "id",
            "op_id",
            "product",
            "type",
            "type_display",
            "quantity",
            "balance_after",
            "household",
            "document_no",
            "note",
            "created_by",
            "created_at",
            "lots",
            "reference",
            "files",
        )
        read_only_fields = fields

    #: An owner sees "his" stock rows without the central warehouse internals (V3 §5.5 "balances are not
    #: exposed to owners"): no company-wide ``balance_after``, no lot numbers/expiry, the actor is "NNP".
    OWNER_ACTOR = {"id": None, "full_name": "NNP"}

    def _owner_view(self) -> bool:
        request = self.context.get("request")
        return bool(self.context.get("owner_view")) or (request is not None and role_of(request.user) == Role.OWNER)

    def to_representation(self, instance: StockTransaction) -> dict[str, Any]:
        data = super().to_representation(instance)
        if self._owner_view():
            data["balance_after"] = None
            data["lots"] = []
            data["created_by"] = dict(self.OWNER_ACTOR)
        return data

    @extend_schema_field(AttachmentSerializer(many=True))
    def get_files(self, row: StockTransaction) -> list[dict[str, Any]]:
        """Attached documents (TZ §7). Lists pass ``files_map`` (one query); otherwise one query per row."""
        files_map = self.context.get("files_map")
        if files_map is None:
            if not attachments_ready():
                return []
            rows = files_for(row)
        else:
            rows = files_map.get(row.pk, [])
        return AttachmentSerializer(rows, many=True).data

    @extend_schema_field(LOT_REF_SCHEMA)
    def get_lots(self, row: StockTransaction) -> list[dict[str, Any]]:
        if not lots_ready() or self._owner_view():
            return []
        if row.type == StockTransactionType.IN:
            try:
                lot = row.lot
            except StockLot.DoesNotExist:
                return []
            return [_lot_entry(lot, lot.received_qty)]
        if row.type in OUTGOING_TYPES:
            return [_lot_entry(item.lot, item.quantity) for item in row.lot_allocations.all()]
        return []

    @extend_schema_field(REFERENCE_SCHEMA)
    def get_reference(self, row: StockTransaction) -> dict[str, Any] | None:
        kind = REFERENCE_KINDS.get(row.reference_type or "")
        if kind is None or row.reference_id is None:
            return None
        prefix, path = kind
        if self._owner_view():
            path = None
            if row.reference_type == "logistics_task":
                orders = self.context.get("task_orders")
                if orders is None:
                    orders = task_order_ids([row])
                order_id = orders.get(row.reference_id)
                path = f"/my/orders/{order_id}" if order_id else "/my/deliveries"
        elif path is not None:
            path = path.format(id=row.reference_id)
        return {"type": row.reference_type, "id": row.reference_id, "code": f"{prefix}-{row.reference_id:06d}",
                "path": path}


class StockTransactionCreateSerializer(serializers.Serializer):
    product = PrimaryKeyField(queryset=Product.objects.all())
    type = serializers.ChoiceField(choices=StockTransactionType.choices)
    quantity = serializers.DecimalField(max_digits=12, decimal_places=2)
    household = PrimaryKeyField(
        queryset=Household.objects.select_related("owner"), required=False, allow_null=True, default=None
    )
    document_no = serializers.CharField(max_length=64)
    note = serializers.CharField(max_length=500, allow_blank=True, required=False, default="")
    #: V3 §5.2 — IN only.
    lot_no = serializers.CharField(max_length=64, allow_blank=True, required=False, default="", trim_whitespace=True)
    expiry_date = serializers.DateField(required=False, allow_null=True, default=None)
    #: V3 §5.2 — OUT only: consume this lot first.
    lot = serializers.IntegerField(required=False, allow_null=True, default=None, min_value=1)
    #: TZ §7: scans of the waybill / invoice (``POST /api/files`` purpose ``STOCK_DOCUMENT``).
    file_ids = FileIdsField()

    def validate_quantity(self, value):
        if value <= 0:
            raise serializers.ValidationError("Miqdor 0 dan katta bo‘lishi kerak")
        return value

    def validate_lot(self, value):
        if value is not None and not fits_db_int(value):
            raise serializers.ValidationError("Tanlangan partiyada yetarli qoldiq yo‘q.")
        return value


# --- Lots (V3 §5.2) ------------------------------------------------------------------------------


class StockLotSerializer(serializers.ModelSerializer):
    product = ProductShortSerializer(read_only=True)
    received_at = serializers.DateTimeField(source="created_at", read_only=True)
    in_transaction = serializers.SerializerMethodField()
    days_to_expiry = serializers.SerializerMethodField()
    status = serializers.SerializerMethodField()
    status_display = serializers.SerializerMethodField()

    class Meta:
        model = StockLot
        fields = (
            "id",
            "product",
            "lot_no",
            "expiry_date",
            "received_qty",
            "remaining_qty",
            "received_at",
            "in_transaction",
            "days_to_expiry",
            "status",
            "status_display",
        )
        read_only_fields = fields

    def _today(self) -> date:
        today = self.context.get("today")
        if today is None:
            today = self.context["today"] = timezone.localdate()
        return today

    def _soon(self) -> int:
        soon = self.context.get("soon_days")
        if soon is None:
            soon = self.context["soon_days"] = lot_reads.expiry_soon_days()
        return soon

    @extend_schema_field(
        inline_serializer("StockLotInTransaction", {"id": serializers.IntegerField(), "op_id": serializers.CharField()})
    )
    def get_in_transaction(self, lot: StockLot) -> dict[str, Any]:
        return {"id": lot.in_transaction_id, "op_id": f"OM-{lot.in_transaction_id:06d}"}

    @extend_schema_field(OpenApiTypes.INT)
    def get_days_to_expiry(self, lot: StockLot) -> int | None:
        return lot_reads.days_to_expiry(lot, self._today())

    @extend_schema_field(serializers.CharField(help_text="EMPTY | EXPIRED | EXPIRING | OK"))
    def get_status(self, lot: StockLot) -> str:
        return lot_reads.lot_status(lot, self._today(), self._soon())

    @extend_schema_field(OpenApiTypes.STR)
    def get_status_display(self, lot: StockLot) -> str:
        return lot_reads.LotStatus.LABELS[self.get_status(lot)]


# --- Inventory count (V3 §5.3) -------------------------------------------------------------------


class InventoryCountShortSerializer(serializers.ModelSerializer):
    code = serializers.CharField(read_only=True)
    status = serializers.CharField(read_only=True, help_text="DRAFT | APPROVED | CANCELLED")
    status_display = serializers.CharField(source="get_status_display", read_only=True)
    lines_total = serializers.IntegerField(read_only=True)
    lines_counted = serializers.IntegerField(read_only=True)
    lines_with_difference = serializers.IntegerField(read_only=True)
    created_by = UserRefSerializer(read_only=True, allow_null=True)

    class Meta:
        model = InventoryCount
        fields = (
            "id",
            "code",
            "status",
            "status_display",
            "note",
            "lines_total",
            "lines_counted",
            "lines_with_difference",
            "created_by",
            "created_at",
            "approved_at",
        )
        read_only_fields = fields


class InventoryCountLineSerializer(serializers.ModelSerializer):
    product = ProductShortSerializer(read_only=True)
    current_balance = serializers.DecimalField(source="product.balance", max_digits=12, decimal_places=2, read_only=True)
    reserved = serializers.DecimalField(source="product.reserved", max_digits=12, decimal_places=2, read_only=True)
    adjustment = serializers.SerializerMethodField()

    class Meta:
        model = InventoryCountLine
        fields = (
            "id",
            "product",
            "expected_qty",
            "current_balance",
            "reserved",
            "counted_qty",
            "balance_at_approval",
            "difference",
            "adjustment",
            "counted_at",
        )
        read_only_fields = fields

    @extend_schema_field(
        inline_serializer(
            "InventoryAdjustmentRef",
            {"id": serializers.IntegerField(), "op_id": serializers.CharField(), "type": serializers.CharField()},
            allow_null=True,
        )
    )
    def get_adjustment(self, line: InventoryCountLine) -> dict[str, Any] | None:
        if line.adjustment_id is None:
            return None
        return {"id": line.adjustment_id, "op_id": f"OM-{line.adjustment_id:06d}", "type": line.adjustment.type}


class InventoryCountSerializer(serializers.ModelSerializer):
    code = serializers.CharField(read_only=True)
    status = serializers.CharField(read_only=True, help_text="DRAFT | APPROVED | CANCELLED")
    status_display = serializers.CharField(source="get_status_display", read_only=True)
    created_by = UserRefSerializer(read_only=True, allow_null=True)
    approved_by = UserRefSerializer(read_only=True, allow_null=True)
    cancelled_by = UserRefSerializer(read_only=True, allow_null=True)
    lines = InventoryCountLineSerializer(many=True, read_only=True)
    allowed_actions = serializers.SerializerMethodField()

    class Meta:
        model = InventoryCount
        fields = (
            "id",
            "code",
            "status",
            "status_display",
            "note",
            "created_by",
            "created_at",
            "approved_by",
            "approved_at",
            "cancelled_by",
            "cancelled_at",
            "cancel_reason",
            "allowed_actions",
            "lines",
        )
        read_only_fields = fields

    @extend_schema_field(serializers.ListField(child=serializers.CharField()))
    def get_allowed_actions(self, count: InventoryCount) -> list[str]:
        """``count`` / ``cancel`` (A, W) and ``approve`` (A) while DRAFT."""
        if count.status != "DRAFT":
            return []
        request = self.context.get("request")
        role = role_of(request.user) if request is not None else None
        actions = ["count", "cancel"] if role in (Role.ADMIN, Role.WAREHOUSE) else []
        if role == Role.ADMIN:
            actions.insert(1, "approve")
        return actions


class InventoryCountCreateSerializer(serializers.Serializer):
    note = serializers.CharField(max_length=500, allow_blank=True, required=False, default="")
    category = serializers.ChoiceField(choices=ProductCategory.choices, required=False, allow_null=True, default=None)
    product_ids = serializers.ListField(
        child=serializers.IntegerField(min_value=1), required=False, allow_null=True, default=None, max_length=1000
    )

    def validate_product_ids(self, value):
        if value is not None:
            if any(not fits_db_int(pk) for pk in value):
                raise serializers.ValidationError("Mahsulot topilmadi.")
            if not value:
                raise serializers.ValidationError("Kamida bitta mahsulot tanlang.")
        return value


class InventoryLineInputSerializer(serializers.Serializer):
    product = serializers.IntegerField(min_value=1)
    counted_qty = serializers.DecimalField(
        max_digits=12, decimal_places=2, allow_null=True, min_value=Decimal("0"), max_value=MAX_AMOUNT
    )

    def validate_product(self, value):
        if not fits_db_int(value):
            raise serializers.ValidationError("Mahsulot topilmadi.")
        return value


class InventoryLinesSerializer(serializers.Serializer):
    lines = InventoryLineInputSerializer(many=True, allow_empty=False, max_length=1000)


class InventoryCancelSerializer(serializers.Serializer):
    reason = serializers.CharField(min_length=3, max_length=500)


def attachments_ready() -> bool:
    """``core_attachment`` exists (a server running before ``migrate`` must not fail on stock lists)."""
    from core.models import Attachment

    from .readiness import models_ready

    return models_ready(Attachment)


def stock_files_map(rows) -> dict | None:
    """``{transaction_id: [Attachment]}`` for a page of operations (one query), or ``None`` before migrate."""
    if not attachments_ready():
        return {}
    return files_by_object("StockTransaction", [row.pk for row in rows])


# --- V4 §5 feed norms and construction options ---------------------------------------------------


class FeedNormSerializer(serializers.ModelSerializer):
    """``FeedNorm`` (read): ``product`` = ``{"id", "name", "unit"}`` or ``null``."""

    poultry_type_display = serializers.CharField(source="get_poultry_type_display", read_only=True)
    product = ProductRefSerializer(read_only=True, allow_null=True)
    grams_per_bird_day = serializers.DecimalField(max_digits=8, decimal_places=2, read_only=True)

    class Meta:
        model = FeedNorm
        fields = (
            "id",
            "code",
            "poultry_type",
            "poultry_type_display",
            "day_from",
            "day_to",
            "grams_per_bird_day",
            "product",
            "note",
            "created_at",
            "updated_at",
        )
        read_only_fields = fields


class FeedNormWriteSerializer(serializers.Serializer):
    """POST (all required except ``product`` / ``note``) and PATCH (partial)."""

    poultry_type = serializers.ChoiceField(choices=PoultryType.choices)
    day_from = serializers.IntegerField(min_value=NORM_DAY_MIN, max_value=NORM_DAY_MAX)
    day_to = serializers.IntegerField(min_value=NORM_DAY_MIN, max_value=NORM_DAY_MAX)
    grams_per_bird_day = serializers.DecimalField(max_digits=8, decimal_places=2, min_value=Decimal("0.01"))
    product = PrimaryKeyField(
        queryset=Product.objects.all(),
        required=False,
        allow_null=True,
        error_messages={"does_not_exist": "Mahsulot topilmadi."},
    )
    note = serializers.CharField(max_length=500, required=False, allow_blank=True, default="")

    def validate_product(self, value: Product | None) -> Product | None:
        if value is not None and value.category != ProductCategory.FEED:
            raise serializers.ValidationError("Faqat “Yem” toifasidagi mahsulotni tanlash mumkin.")
        return value

    def validate(self, attrs: dict) -> dict:
        if self.partial and "note" not in self.initial_data:
            attrs.pop("note", None)
        day_from, day_to = attrs.get("day_from"), attrs.get("day_to")
        if day_from is not None and day_to is not None and day_from > day_to:
            raise serializers.ValidationError({"day_to": ["“Kungacha” “Kundan”dan kichik bo‘lmasligi kerak."]})
        return attrs


class ConstructionOptionSerializer(serializers.ModelSerializer):
    price = serializers.DecimalField(max_digits=14, decimal_places=2, min_value=0, required=False, allow_null=True)
    name = serializers.CharField(max_length=200)
    description = serializers.CharField(max_length=1000, required=False, allow_blank=True)
    unit = serializers.CharField(max_length=32, required=False, allow_blank=True)

    class Meta:
        model = ConstructionOption
        fields = ("id", "code", "name", "description", "unit", "price", "is_active", "created_at", "updated_at")
        read_only_fields = ("id", "code", "created_at", "updated_at")
        extra_kwargs = {"is_active": {"required": False}}

    def validate_name(self, value: str) -> str:
        value = value.strip()
        if not value:
            raise serializers.ValidationError("Bu maydon bo‘sh bo‘lishi mumkin emas.")
        duplicates = ConstructionOption.objects.filter(name__iexact=value)
        if self.instance is not None:
            duplicates = duplicates.exclude(pk=self.instance.pk)
        if duplicates.exists():
            raise serializers.ValidationError(MSG_OPTION_DUPLICATE)
        return value

    def save(self, **kwargs):
        raise NotImplementedError("Use warehouse.norms (config versions + audit).")

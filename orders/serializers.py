"""DRF serializers of the orders app (API_CONTRACT_V3 §6.3, §6.4, §5.5).

Choice values are exposed as plain strings (``kind``, ``status``) so the OpenAPI schema needs no new
``ENUM_NAME_OVERRIDES`` entry (``config/settings.py`` is frozen for module teams).
"""

from __future__ import annotations

from typing import Any

from django.utils import timezone
from drf_spectacular.utils import extend_schema_field
from rest_framework import serializers

from accounts.serializers import UserRefSerializer
from common.fields import PrimaryKeyField
from farm.models import Household
from farm.serializers import HouseholdShortSerializer
from warehouse.models import MAX_AMOUNT, Product

from . import selectors
from .models import ITEM_NOTE_MAX, MAX_ITEMS, NOTE_MAX, Order, OrderItem, OrderKind, OrderStatusLog
from .services import (
    MSG_DESIRED_DATE_PAST,
    MSG_ITEMS_DUPLICATE,
    MSG_ITEMS_EMPTY,
    MSG_ITEMS_TOO_MANY,
    MSG_QTY_POSITIVE,
    MSG_REASON_REQUIRED,
    ItemInput,
    msg_inactive_product,
)

MSG_KIND = "Buyurtma turini tanlang."
MSG_REASON_TOO_LONG = f"Sabab {NOTE_MAX} ta belgidan oshmasin."


# --- Output -----------------------------------------------------------------------------------


class OrderProductSerializer(serializers.ModelSerializer):
    class Meta:
        model = Product
        fields = ("id", "name", "unit", "category")


class OrderItemSerializer(serializers.ModelSerializer):
    product = OrderProductSerializer(read_only=True)
    requested_qty = serializers.FloatField(read_only=True)
    approved_qty = serializers.FloatField(read_only=True, allow_null=True)

    class Meta:
        model = OrderItem
        fields = ("id", "product", "requested_qty", "approved_qty", "note")
        read_only_fields = fields


class TaskRefSerializer(serializers.Serializer):
    id = serializers.IntegerField()
    code = serializers.CharField()
    status = serializers.CharField()
    status_display = serializers.CharField()


class HistoryActorSerializer(serializers.Serializer):
    id = serializers.IntegerField(allow_null=True)
    full_name = serializers.CharField()


class OrderHistorySerializer(serializers.ModelSerializer):
    to_status = serializers.CharField(read_only=True)
    to_status_display = serializers.SerializerMethodField()
    actor = serializers.SerializerMethodField()

    class Meta:
        model = OrderStatusLog
        fields = ("from_status", "to_status", "to_status_display", "actor", "note", "created_at")
        read_only_fields = fields

    def get_to_status_display(self, row: OrderStatusLog) -> str:
        return row.get_to_status_display()

    @extend_schema_field(HistoryActorSerializer)
    def get_actor(self, row: OrderStatusLog) -> dict[str, Any]:
        if row.actor_id is not None and row.actor is not None:
            return {"id": row.actor_id, "full_name": row.actor.full_name}
        return {"id": None, "full_name": row.actor_name or "NNP"}


class OrderSerializer(serializers.ModelSerializer):
    """Order object (§6.3). Context: ``request`` (allowed actions), ``tasks`` (``{order_id: task}``)."""

    code = serializers.CharField(read_only=True)
    kind = serializers.CharField(read_only=True)
    kind_display = serializers.CharField(source="get_kind_display", read_only=True)
    household = HouseholdShortSerializer(read_only=True)
    status = serializers.CharField(read_only=True)
    status_display = serializers.CharField(source="get_status_display", read_only=True)
    items = OrderItemSerializer(many=True, read_only=True)
    created_by = UserRefSerializer(read_only=True)
    decided_by = UserRefSerializer(read_only=True, allow_null=True)
    task = serializers.SerializerMethodField()
    allowed_actions = serializers.SerializerMethodField()

    class Meta:
        model = Order
        fields = (
            "id",
            "code",
            "kind",
            "kind_display",
            "household",
            "status",
            "status_display",
            "note",
            "desired_date",
            "items",
            "created_by",
            "created_at",
            "decided_by",
            "decided_at",
            "decision_note",
            "cancel_reason",
            "task",
            "allowed_actions",
        )
        read_only_fields = fields

    def _task(self, order: Order):
        return (self.context.get("tasks") or {}).get(order.pk)

    @extend_schema_field(TaskRefSerializer(allow_null=True))
    def get_task(self, order: Order) -> dict[str, Any] | None:
        from .bridge import task_ref

        return task_ref(self._task(order))

    @extend_schema_field(serializers.ListField(child=serializers.CharField()))
    def get_allowed_actions(self, order: Order) -> list[str]:
        request = self.context.get("request")
        user = getattr(request, "user", None)
        return selectors.allowed_actions(order, user, self._task(order), self.context.get("logistics_ready"))


class OrderDetailSerializer(OrderSerializer):
    history = OrderHistorySerializer(source="status_logs", many=True, read_only=True)

    class Meta(OrderSerializer.Meta):
        fields = (*OrderSerializer.Meta.fields, "history")
        read_only_fields = fields


class CatalogProductSerializer(serializers.ModelSerializer):
    category_display = serializers.CharField(source="get_category_display", read_only=True)
    in_stock = serializers.BooleanField(read_only=True)

    class Meta:
        model = Product
        fields = ("id", "name", "category", "category_display", "unit", "in_stock")
        read_only_fields = fields


# --- Input ------------------------------------------------------------------------------------


class OrderItemInputSerializer(serializers.Serializer):
    product = serializers.IntegerField(min_value=1, max_value=2**63 - 1)
    quantity = serializers.DecimalField(max_digits=12, decimal_places=2)
    note = serializers.CharField(max_length=ITEM_NOTE_MAX, allow_blank=True, required=False, default="")


class OrderCreateSerializer(serializers.Serializer):
    kind = serializers.CharField(error_messages={"required": MSG_KIND, "blank": MSG_KIND})
    items = serializers.ListField(child=OrderItemInputSerializer(), allow_empty=True)
    note = serializers.CharField(max_length=NOTE_MAX, allow_blank=True, required=False, default="")
    desired_date = serializers.DateField(required=False, allow_null=True, default=None)
    household = PrimaryKeyField(
        queryset=Household.objects.select_related("owner"), required=False, allow_null=True, default=None
    )

    def validate_kind(self, value: str) -> str:
        if value not in OrderKind.values:
            raise serializers.ValidationError(f"“{value}” ruxsat etilgan qiymat emas.")
        return value

    def validate_desired_date(self, value):
        if value is not None and value < timezone.localdate():
            raise serializers.ValidationError(MSG_DESIRED_DATE_PAST)
        return value

    def validate_items(self, rows: list[dict[str, Any]]) -> list[ItemInput]:
        if not rows:
            raise serializers.ValidationError(MSG_ITEMS_EMPTY)
        if len(rows) > MAX_ITEMS:
            raise serializers.ValidationError(MSG_ITEMS_TOO_MANY)
        ids = [row["product"] for row in rows]
        if len(set(ids)) != len(ids):
            raise serializers.ValidationError(MSG_ITEMS_DUPLICATE)
        products = Product.objects.in_bulk(ids)
        result: list[ItemInput] = []
        for row in rows:
            product = products.get(row["product"])
            if product is None:
                raise serializers.ValidationError(msg_inactive_product(f"#{row['product']}"))
            if not product.is_active:
                raise serializers.ValidationError(msg_inactive_product(product.name))
            if row["quantity"] <= 0 or row["quantity"] > MAX_AMOUNT:
                raise serializers.ValidationError(MSG_QTY_POSITIVE)
            result.append(ItemInput(product=product, quantity=row["quantity"], note=row.get("note", "").strip()))
        return result


class ApproveItemSerializer(serializers.Serializer):
    id = serializers.IntegerField(min_value=1, max_value=2**63 - 1)
    approved_qty = serializers.DecimalField(max_digits=12, decimal_places=2)


class TaskDataSerializer(serializers.Serializer):
    """Optional task fields; ids are validated by the logistics service (active vehicle / driver ...)."""

    planned_date = serializers.DateField(required=False, allow_null=True)
    vehicle = serializers.IntegerField(required=False, allow_null=True, min_value=1, max_value=2**63 - 1)
    driver = serializers.IntegerField(required=False, allow_null=True, min_value=1, max_value=2**63 - 1)
    responsible = serializers.IntegerField(required=False, allow_null=True, min_value=1, max_value=2**63 - 1)
    note = serializers.CharField(max_length=NOTE_MAX, allow_blank=True, required=False)

    def validate_planned_date(self, value):
        if value is not None and value < timezone.localdate():
            raise serializers.ValidationError(MSG_DESIRED_DATE_PAST)
        return value


class OrderApproveSerializer(serializers.Serializer):
    items = serializers.ListField(child=ApproveItemSerializer(), allow_empty=False, max_length=MAX_ITEMS)
    decision_note = serializers.CharField(max_length=NOTE_MAX, allow_blank=True, required=False, default="")
    create_task = serializers.BooleanField(required=False, default=True)
    task = TaskDataSerializer(required=False, allow_null=True, default=None)


class ReasonSerializer(serializers.Serializer):
    """``{reason}``: required 3–500 unless ``optional`` (owner cancel)."""

    reason = serializers.CharField(allow_blank=True, required=False, default="", trim_whitespace=True)

    def __init__(self, *args, optional: bool = False, **kwargs) -> None:
        self.optional = optional
        super().__init__(*args, **kwargs)

    def validate_reason(self, value: str) -> str:
        if len(value) > NOTE_MAX:
            raise serializers.ValidationError(MSG_REASON_TOO_LONG)
        if not self.optional and len(value) < 3:
            raise serializers.ValidationError(MSG_REASON_REQUIRED)
        return value



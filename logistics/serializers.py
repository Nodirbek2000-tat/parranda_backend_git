"""Serializers of the logistics app (API_CONTRACT_V3 §7, §14.1).

Status fields are plain strings in the schema (no enum) so the OpenAPI enum names stay unique without new
``ENUM_NAME_OVERRIDES`` entries; the values are documented in the contract.
"""

from __future__ import annotations

import re
from decimal import Decimal
from typing import Any

from django.utils import timezone
from drf_spectacular.utils import extend_schema_field
from rest_framework import serializers

from accounts.models import User
from accounts.serializers import validate_phone
from common.fields import PrimaryKeyField, latitude_field, longitude_field
from common.roles import Role, role_of
from core import files
from core.serializers import AttachmentSerializer
from farm.models import Household
from warehouse.models import MAX_AMOUNT, Product

from . import events, hooks, services
from .models import Driver, LogisticsTask, LogisticsTaskStatusLog, Vehicle

PLATE_RE = re.compile(r"^[0-9A-Z ]{5,15}$")
MSG_PLATE = "Davlat raqami 5–15 ta lotin harfi yoki raqamdan iborat bo‘lsin (masalan, 01 A 123 BC)."
NNP_ACTOR = {"id": None, "full_name": "NNP"}


def _num(value: Decimal | None) -> float | None:
    return None if value is None else float(value)


def _user_ref(user: User | None) -> dict | None:
    return {"id": user.pk, "full_name": user.full_name} if user is not None else None


def _is_owner(context: dict) -> bool:
    request = context.get("request")
    user = getattr(request, "user", None) or context.get("user")
    return role_of(user) == Role.OWNER


def _context_user(context: dict) -> User | None:
    request = context.get("request")
    return context.get("user") or getattr(request, "user", None)


# --- Vehicles and drivers ------------------------------------------------------------------------


class VehicleSerializer(serializers.ModelSerializer):
    capacity_kg = serializers.SerializerMethodField()
    active_tasks = serializers.SerializerMethodField()

    class Meta:
        model = Vehicle
        fields = ("id", "plate_number", "model", "capacity_kg", "is_active", "note", "active_tasks", "created_at")
        read_only_fields = fields

    def get_capacity_kg(self, obj: Vehicle) -> float | None:
        return _num(obj.capacity_kg)

    def get_active_tasks(self, obj: Vehicle) -> int:
        value = getattr(obj, "active_tasks", None)
        if value is None:
            value = obj.tasks.filter(status__in=("ASSIGNED", "ACCEPTED", "IN_TRANSIT")).count()
        return value


def normalize_plate(value: str) -> str:
    return " ".join((value or "").upper().split())


class VehicleWriteSerializer(serializers.Serializer):
    plate_number = serializers.CharField(max_length=40)
    model = serializers.CharField(max_length=120, required=False, allow_blank=True, default="")
    capacity_kg = serializers.DecimalField(
        max_digits=10, decimal_places=2, min_value=Decimal("0.01"), required=False, allow_null=True, default=None
    )
    is_active = serializers.BooleanField(required=False, default=True)
    note = serializers.CharField(max_length=500, required=False, allow_blank=True, default="")

    def validate_plate_number(self, value: str) -> str:
        plate = normalize_plate(value)
        if not PLATE_RE.fullmatch(plate):
            raise serializers.ValidationError(MSG_PLATE)
        return plate

    def validate_model(self, value: str) -> str:
        return value.strip()

    def validate_note(self, value: str) -> str:
        return value.strip()


class DriverUserSerializer(serializers.ModelSerializer):
    full_name = serializers.CharField(read_only=True)
    telegram_linked = serializers.BooleanField(read_only=True)

    class Meta:
        model = User
        fields = ("id", "full_name", "username", "telegram_linked")


class DriverSerializer(serializers.ModelSerializer):
    user = DriverUserSerializer(read_only=True, allow_null=True)
    active_tasks = serializers.SerializerMethodField()

    class Meta:
        model = Driver
        fields = ("id", "full_name", "phone", "license_no", "user", "is_active", "note", "active_tasks", "created_at")
        read_only_fields = fields

    def get_active_tasks(self, obj: Driver) -> int:
        value = getattr(obj, "active_tasks", None)
        if value is None:
            value = obj.tasks.filter(status__in=("ASSIGNED", "ACCEPTED", "IN_TRANSIT")).count()
        return value


class DriverWriteSerializer(serializers.Serializer):
    full_name = serializers.CharField(min_length=3, max_length=200)
    phone = serializers.CharField(max_length=32, validators=[validate_phone])
    license_no = serializers.CharField(max_length=32, required=False, allow_blank=True, default="")
    user = PrimaryKeyField(
        queryset=User.objects.filter(deleted_at__isnull=True), required=False, allow_null=True, default=None
    )
    is_active = serializers.BooleanField(required=False, default=True)
    note = serializers.CharField(max_length=500, required=False, allow_blank=True, default="")

    def validate_full_name(self, value: str) -> str:
        value = " ".join(value.split())
        if len(value) < 3:
            raise serializers.ValidationError("Kamida 3 ta belgi kiriting.")
        return value

    def validate_license_no(self, value: str) -> str:
        return value.strip().upper()


# --- Tasks ---------------------------------------------------------------------------------------


class TaskOrderRefSerializer(serializers.Serializer):
    id = serializers.IntegerField()
    code = serializers.CharField()


class TaskItemSerializer(serializers.Serializer):
    id = serializers.IntegerField()
    product = serializers.SerializerMethodField()
    quantity = serializers.SerializerMethodField()
    reserved_qty = serializers.SerializerMethodField()
    stock_transaction = serializers.SerializerMethodField()
    return_transaction = serializers.SerializerMethodField()

    @extend_schema_field(
        {"type": "object", "properties": {"id": {"type": "integer"}, "name": {"type": "string"},
                                          "unit": {"type": "string"}, "category": {"type": "string"}}}
    )
    def get_product(self, obj) -> dict:
        product = obj.product
        return {"id": product.pk, "name": product.name, "unit": product.unit, "category": product.category}

    def get_quantity(self, obj) -> float:
        return float(obj.quantity)

    def get_reserved_qty(self, obj) -> float:
        return float(obj.reserved_qty)

    @extend_schema_field({"type": "object", "nullable": True,
                          "properties": {"id": {"type": "integer"}, "op_id": {"type": "string"}}})
    def get_stock_transaction(self, obj) -> dict | None:
        return {"id": obj.stock_transaction_id, "op_id": f"OM-{obj.stock_transaction_id:06d}"} \
            if obj.stock_transaction_id else None

    @extend_schema_field({"type": "object", "nullable": True,
                          "properties": {"id": {"type": "integer"}, "op_id": {"type": "string"}}})
    def get_return_transaction(self, obj) -> dict | None:
        return {"id": obj.return_transaction_id, "op_id": f"OM-{obj.return_transaction_id:06d}"} \
            if obj.return_transaction_id else None


class TaskStatusLogSerializer(serializers.ModelSerializer):
    to_status_display = serializers.CharField(source="get_to_status_display", read_only=True)
    from_status_display = serializers.SerializerMethodField()
    actor = serializers.SerializerMethodField()
    source = serializers.CharField(read_only=True)
    from_status = serializers.CharField(read_only=True)
    to_status = serializers.CharField(read_only=True)
    latitude = latitude_field(read_only=True)
    longitude = longitude_field(read_only=True)

    class Meta:
        model = LogisticsTaskStatusLog
        fields = ("id", "from_status", "from_status_display", "to_status", "to_status_display", "actor", "source",
                  "note", "latitude", "longitude", "created_at")
        read_only_fields = fields

    def get_from_status_display(self, obj) -> str:
        from .models import TaskStatus

        return TaskStatus(obj.from_status).label if obj.from_status in TaskStatus.values else ""

    @extend_schema_field({"type": "object", "properties": {"id": {"type": "integer", "nullable": True},
                                                           "full_name": {"type": "string"}}})
    def get_actor(self, obj) -> dict:
        if _is_owner(self.context):
            return NNP_ACTOR
        return {"id": obj.actor_id, "full_name": obj.actor_name or (obj.actor.full_name if obj.actor else "")}


class LogisticsTaskSerializer(serializers.ModelSerializer):
    """The §7.3 object. ``context["files"]`` = ``{task_id: [Attachment]}`` (one query per page)."""

    status = serializers.CharField(read_only=True)
    status_display = serializers.CharField(source="get_status_display", read_only=True)
    order = serializers.SerializerMethodField()
    household = serializers.SerializerMethodField()
    delivery = serializers.SerializerMethodField()
    items = serializers.SerializerMethodField()
    total_kg = serializers.SerializerMethodField()
    is_overdue = serializers.SerializerMethodField()
    vehicle = serializers.SerializerMethodField()
    driver = serializers.SerializerMethodField()
    responsible = serializers.SerializerMethodField()
    created_by = serializers.SerializerMethodField()
    proof_files = serializers.SerializerMethodField()
    allowed_actions = serializers.SerializerMethodField()

    class Meta:
        model = LogisticsTask
        fields = (
            "id", "code", "status", "status_display", "order", "household", "delivery", "items", "total_kg",
            "planned_date", "is_overdue", "vehicle", "driver", "responsible", "note", "delivery_note",
            "recipient_name", "received_ok", "issue_note", "cancel_reason", "proof_files", "created_by",
            "created_at", "assigned_at", "accepted_at", "started_at", "delivered_at", "confirmed_at",
            "cancelled_at", "allowed_actions",
        )
        read_only_fields = fields

    def to_representation(self, instance):
        data = super().to_representation(instance)
        if _is_owner(self.context):
            data.pop("responsible", None)
            data.pop("created_by", None)
        return data

    @extend_schema_field(TaskOrderRefSerializer(allow_null=True))
    def get_order(self, obj) -> dict | None:
        return hooks.order_ref(obj.order_id)

    @extend_schema_field({"type": "object", "properties": {"id": {"type": "integer"}, "code": {"type": "string"},
                                                           "name": {"type": "string"}}})
    def get_household(self, obj) -> dict:
        household = obj.household
        return {"id": household.pk, "code": household.code, "name": household.name}

    @extend_schema_field({"type": "object"})
    def get_delivery(self, obj) -> dict:
        return {
            "contact_name": obj.contact_name,
            "contact_phone": obj.contact_phone,
            "address": obj.address_text,
            "latitude": _num(obj.latitude),
            "longitude": _num(obj.longitude),
        }

    @extend_schema_field(TaskItemSerializer(many=True))
    def get_items(self, obj) -> list:
        return TaskItemSerializer(list(obj.items.all()), many=True).data

    def get_total_kg(self, obj) -> float:
        return float(sum((item.quantity for item in obj.items.all() if item.product.unit == "kg"), Decimal("0")))

    def get_is_overdue(self, obj) -> bool:
        return (
            obj.planned_date is not None
            and obj.planned_date < timezone.localdate()
            and obj.status in ("NEW", "ASSIGNED", "ACCEPTED", "IN_TRANSIT")
        )

    @extend_schema_field({"type": "object", "nullable": True})
    def get_vehicle(self, obj) -> dict | None:
        vehicle = obj.vehicle
        if vehicle is None:
            return None
        return {"id": vehicle.pk, "plate_number": vehicle.plate_number, "model": vehicle.model,
                "capacity_kg": _num(vehicle.capacity_kg)}

    @extend_schema_field({"type": "object", "nullable": True})
    def get_driver(self, obj) -> dict | None:
        driver = obj.driver
        if driver is None:
            return None
        return {"id": driver.pk, "full_name": driver.full_name, "phone": driver.phone}

    @extend_schema_field({"type": "object", "nullable": True})
    def get_responsible(self, obj) -> dict | None:
        return _user_ref(obj.responsible)

    @extend_schema_field({"type": "object", "nullable": True})
    def get_created_by(self, obj) -> dict | None:
        return _user_ref(obj.created_by)

    @extend_schema_field(AttachmentSerializer(many=True))
    def get_proof_files(self, obj) -> list:
        by_id = self.context.get("files")
        rows = by_id.get(obj.pk, []) if by_id is not None else files.files_for(obj)
        return AttachmentSerializer(rows, many=True).data

    def get_allowed_actions(self, obj) -> list[str]:
        user = _context_user(self.context)
        return services.allowed_actions(obj, user) if user is not None else []


class LogisticsTaskDetailSerializer(LogisticsTaskSerializer):
    history = serializers.SerializerMethodField()

    class Meta(LogisticsTaskSerializer.Meta):
        fields = (*LogisticsTaskSerializer.Meta.fields, "history")
        read_only_fields = fields

    @extend_schema_field(TaskStatusLogSerializer(many=True))
    def get_history(self, obj) -> list:
        rows = obj.history.select_related("actor").order_by("created_at", "id")
        return TaskStatusLogSerializer(rows, many=True, context=self.context).data


class LogisticsTaskShortSerializer(serializers.ModelSerializer):
    """``LogisticsTaskShort`` of the bot (§14.1)."""

    status = serializers.CharField(read_only=True)
    status_display = serializers.CharField(source="get_status_display", read_only=True)
    household = serializers.SerializerMethodField()
    address = serializers.CharField(source="address_text", read_only=True)
    items_text = serializers.SerializerMethodField()
    vehicle_text = serializers.SerializerMethodField()
    driver_text = serializers.SerializerMethodField()
    is_overdue = serializers.SerializerMethodField()
    order = serializers.SerializerMethodField()
    allowed_actions = serializers.SerializerMethodField()

    class Meta:
        model = LogisticsTask
        fields = ("id", "code", "status", "status_display", "household", "address", "planned_date", "is_overdue",
                  "order", "items_text", "vehicle_text", "driver_text", "allowed_actions")
        read_only_fields = fields

    @extend_schema_field({"type": "object"})
    def get_household(self, obj) -> dict:
        return {"id": obj.household.pk, "code": obj.household.code, "name": obj.household.name}

    def get_items_text(self, obj) -> str:
        from notifications.compose import items_text

        return items_text([(item.product.name, item.quantity, item.product.unit) for item in obj.items.all()])

    def get_vehicle_text(self, obj) -> str:
        return events.vehicle_text(obj)

    def get_driver_text(self, obj) -> str:
        return events.driver_text(obj)

    def get_is_overdue(self, obj) -> bool:
        return LogisticsTaskSerializer.get_is_overdue(self, obj)

    @extend_schema_field(TaskOrderRefSerializer(allow_null=True))
    def get_order(self, obj) -> dict | None:
        return hooks.order_ref(obj.order_id)

    def get_allowed_actions(self, obj) -> list[str]:
        user = _context_user(self.context)
        return services.allowed_actions(obj, user) if user is not None else []


# --- Write serializers ---------------------------------------------------------------------------


class TaskItemInputSerializer(serializers.Serializer):
    product = PrimaryKeyField(queryset=Product.objects.all())
    quantity = serializers.DecimalField(max_digits=12, decimal_places=2, max_value=MAX_AMOUNT)


class TaskCreateSerializer(serializers.Serializer):
    household = PrimaryKeyField(queryset=Household.objects.all())
    items = TaskItemInputSerializer(many=True, allow_empty=True)
    planned_date = serializers.DateField(required=False, allow_null=True, default=None)
    note = serializers.CharField(max_length=500, required=False, allow_blank=True, default="")
    vehicle = PrimaryKeyField(queryset=Vehicle.objects.all(), required=False, allow_null=True, default=None)
    driver = PrimaryKeyField(queryset=Driver.objects.all(), required=False, allow_null=True, default=None)
    responsible = PrimaryKeyField(queryset=User.objects.all(), required=False, allow_null=True, default=None)


class TaskUpdateSerializer(serializers.Serializer):
    planned_date = serializers.DateField(required=False, allow_null=True)
    note = serializers.CharField(max_length=500, required=False, allow_blank=True)


class AssignSerializer(serializers.Serializer):
    vehicle = PrimaryKeyField(queryset=Vehicle.objects.all())
    driver = PrimaryKeyField(queryset=Driver.objects.all())
    responsible = PrimaryKeyField(queryset=User.objects.all(), required=False, allow_null=True, default=None)


class NoteSerializer(serializers.Serializer):
    note = serializers.CharField(max_length=1000, required=False, allow_blank=True, default="")


class _CoordinatesMixin(serializers.Serializer):
    latitude = latitude_field(required=False, allow_null=True, default=None)
    longitude = longitude_field(required=False, allow_null=True, default=None)

    def validate(self, attrs: dict[str, Any]) -> dict[str, Any]:
        if (attrs.get("latitude") is None) != (attrs.get("longitude") is None):
            raise serializers.ValidationError({"latitude": ["Kenglik va uzunlik birga kiritilishi kerak"]})
        return attrs


class StartSerializer(_CoordinatesMixin):
    note = serializers.CharField(max_length=1000, required=False, allow_blank=True, default="")


class DeliverSerializer(_CoordinatesMixin):
    file_ids = files.FileIdsField()
    recipient_name = serializers.CharField(max_length=200, required=False, allow_blank=True, default="")
    note = serializers.CharField(max_length=500, required=False, allow_blank=True, default="")


class ConfirmSerializer(serializers.Serializer):
    received_ok = serializers.BooleanField(required=False, allow_null=True, default=None)
    issue_note = serializers.CharField(max_length=1000, required=False, allow_blank=True, default="")


class CancelSerializer(serializers.Serializer):
    reason = serializers.CharField(max_length=500, required=False, allow_blank=True, default="")


class SummarySerializer(serializers.Serializer):
    date = serializers.DateField()
    by_status = serializers.DictField(child=serializers.IntegerField())
    mine = serializers.DictField(child=serializers.IntegerField())
    overdue = serializers.IntegerField()
    delivered_today = serializers.IntegerField()
    confirmed_today = serializers.IntegerField()
    awaiting_confirmation = serializers.IntegerField()
    new_unassigned = serializers.IntegerField()
    last_30d = serializers.DictField()

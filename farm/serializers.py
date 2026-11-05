from __future__ import annotations

from collections.abc import Mapping

from rest_framework import serializers

from accounts.serializers import OwnerShortSerializer, UserRefSerializer, validate_new_username, validate_phone
from common.fields import PrimaryKeyField, latitude_field, longitude_field

from . import selectors
from .models import DailyReport, Household, MovementType, PoultryBatch, PoultryMovement

#: Upper bound for any bird / egg count accepted by the API (keeps aggregates far from overflow).
MAX_BIRDS = 10_000_000
MAX_AGE_DAYS = 3650
MSG_COORDINATES_TOGETHER = "Kenglik va uzunlik birga kiritilishi kerak"


class HouseholdShortSerializer(serializers.ModelSerializer):
    code = serializers.CharField(read_only=True)

    class Meta:
        model = Household
        fields = ("id", "code", "name")


class HouseholdSerializer(serializers.ModelSerializer):
    code = serializers.CharField(read_only=True)
    owner = OwnerShortSerializer(read_only=True)
    latitude = latitude_field(read_only=True)
    longitude = longitude_field(read_only=True)
    bird_count = serializers.SerializerMethodField()

    class Meta:
        model = Household
        fields = (
            "id",
            "code",
            "name",
            "owner",
            "phone",
            "region",
            "district",
            "mfy",
            "address",
            "latitude",
            "longitude",
            "is_active",
            "contract_info",
            "is_demo",
            "bird_count",
            "created_at",
        )
        read_only_fields = fields

    def get_bird_count(self, obj: Household) -> int:
        annotated = getattr(obj, "bird_count", None)
        return annotated if annotated is not None else selectors.household_bird_count(obj.pk)


class HouseholdCreateSerializer(serializers.Serializer):
    name = serializers.CharField(max_length=200)
    owner_first_name = serializers.CharField(max_length=150)
    owner_last_name = serializers.CharField(max_length=150)
    owner_username = serializers.CharField(max_length=150, validators=[validate_new_username])
    phone = serializers.CharField(max_length=32, validators=[validate_phone])
    region = serializers.CharField(max_length=120)
    district = serializers.CharField(max_length=120)
    mfy = serializers.CharField(max_length=120, allow_blank=True, default="")
    address = serializers.CharField(max_length=255, allow_blank=True, default="")
    latitude = latitude_field(required=False, allow_null=True, default=None)
    longitude = longitude_field(required=False, allow_null=True, default=None)
    contract_info = serializers.CharField(allow_blank=True, required=False, default="", max_length=5000)

    def validate(self, attrs: dict) -> dict:
        if (attrs.get("latitude") is None) != (attrs.get("longitude") is None):
            raise serializers.ValidationError({"latitude": [MSG_COORDINATES_TOGETHER]})
        return attrs


class HouseholdUpdateSerializer(serializers.ModelSerializer):
    phone = serializers.CharField(max_length=32, required=False, validators=[validate_phone])
    latitude = latitude_field(required=False, allow_null=True)
    longitude = longitude_field(required=False, allow_null=True)

    class Meta:
        model = Household
        fields = (
            "name",
            "phone",
            "region",
            "district",
            "mfy",
            "address",
            "latitude",
            "longitude",
            "is_active",
            "contract_info",
            "is_demo",
        )

    def validate(self, attrs: dict) -> dict:
        # The resulting household must have both coordinates or neither (the map needs both).
        latitude = attrs.get("latitude", self.instance.latitude)
        longitude = attrs.get("longitude", self.instance.longitude)
        if (latitude is None) != (longitude is None):
            raise serializers.ValidationError({"latitude": [MSG_COORDINATES_TOGETHER]})
        return attrs


class HouseholdBatchSerializer(serializers.Serializer):
    batch_id = serializers.IntegerField()
    batch_code = serializers.CharField()
    poultry_type = serializers.CharField()
    poultry_type_display = serializers.CharField()
    breed = serializers.CharField()
    count = serializers.IntegerField()
    first_received_date = serializers.DateField(allow_null=True)


class OwnerBatchVaccinationSerializer(serializers.Serializer):
    id = serializers.IntegerField()
    code = serializers.CharField()
    vaccine_name = serializers.CharField()
    status = serializers.CharField()
    status_display = serializers.CharField()
    planned_date = serializers.DateField()
    done_date = serializers.DateField(allow_null=True)
    dose = serializers.CharField()


class OwnerBatchSerializer(serializers.Serializer):
    """``GET /api/my/batches/{id}`` — a batch kept in the owner's household (TZ §2 ROLE 6, §6)."""

    id = serializers.IntegerField()
    code = serializers.CharField()
    poultry_type = serializers.CharField()
    poultry_type_display = serializers.CharField()
    breed = serializers.CharField()
    supplier = serializers.CharField()
    arrived_at = serializers.DateField()
    age_days_at_arrival = serializers.IntegerField()
    age_days = serializers.IntegerField(help_text="Bugungi yoshi: kelgandagi yosh + kelgandan beri kunlar")
    health_status = serializers.CharField()
    count = serializers.IntegerField(help_text="Xonadondagi soni")
    first_received_date = serializers.DateField(allow_null=True)
    vaccinations = OwnerBatchVaccinationSerializer(many=True)


class BatchRefSerializer(serializers.ModelSerializer):
    class Meta:
        model = PoultryBatch
        fields = ("id", "code")


class BatchSerializer(serializers.ModelSerializer):
    poultry_type_display = serializers.CharField(source="get_poultry_type_display", read_only=True)
    center_stock = serializers.IntegerField(read_only=True)
    distributed_total = serializers.IntegerField(read_only=True)
    deaths_total = serializers.IntegerField(read_only=True)
    initial_weight_g = serializers.DecimalField(
        max_digits=8, decimal_places=2, min_value=0, required=False, allow_null=True
    )

    class Meta:
        model = PoultryBatch
        fields = (
            "id",
            "code",
            "poultry_type",
            "poultry_type_display",
            "breed",
            "initial_quantity",
            "age_days",
            "supplier",
            "arrived_at",
            "initial_weight_g",
            "health_status",
            "center_stock",
            "distributed_total",
            "deaths_total",
            "created_at",
        )
        read_only_fields = ("id", "code", "created_at")
        extra_kwargs = {
            "initial_quantity": {"min_value": 1, "max_value": MAX_BIRDS},
            "age_days": {"min_value": 0, "max_value": MAX_AGE_DAYS},
            "health_status": {"required": False},
        }

    def validate_initial_weight_g(self, value):
        if value is not None and value <= 0:
            raise serializers.ValidationError("Vazn 0 dan katta bo‘lishi kerak")
        return value


MSG_NOT_EDITABLE = "Bu maydonni o‘zgartirib bo‘lmaydi."
MSG_EMPTY_PATCH = "O‘zgartirish uchun kamida bitta maydon yuboring."


class StrictCharField(serializers.CharField):
    """Only JSON strings (DRF's ``CharField`` would silently accept numbers)."""

    def to_internal_value(self, data) -> str:
        if not isinstance(data, str):
            self.fail("invalid")
        return super().to_internal_value(data)


class BatchUpdateSerializer(serializers.Serializer):
    """``PATCH /api/batches/{id}``: strict whitelist (API_CONTRACT_V2 §4)."""

    health_status = StrictCharField(max_length=120, required=False)
    supplier = StrictCharField(max_length=200, required=False)

    def to_internal_value(self, data):
        if isinstance(data, Mapping):
            unknown = [key for key in data if key not in self.fields]
            if unknown:
                raise serializers.ValidationError({key: [MSG_NOT_EDITABLE] for key in unknown})
            if not data:
                raise serializers.ValidationError({"non_field_errors": [MSG_EMPTY_PATCH]})
        return super().to_internal_value(data)


class MovementSerializer(serializers.ModelSerializer):
    op_id = serializers.CharField(read_only=True)
    batch = BatchRefSerializer(read_only=True)
    household = HouseholdShortSerializer(read_only=True, allow_null=True)
    type_display = serializers.CharField(source="get_type_display", read_only=True)
    created_by = UserRefSerializer(read_only=True, allow_null=True)

    class Meta:
        model = PoultryMovement
        fields = (
            "id",
            "op_id",
            "batch",
            "household",
            "type",
            "type_display",
            "quantity",
            "date",
            "note",
            "created_by",
            "created_at",
        )
        read_only_fields = fields


class MovementCreateSerializer(serializers.Serializer):
    batch = PrimaryKeyField(queryset=PoultryBatch.objects.all())
    household = PrimaryKeyField(
        queryset=Household.objects.select_related("owner"), required=False, allow_null=True, default=None
    )
    type = serializers.ChoiceField(choices=MovementType.choices)
    quantity = serializers.IntegerField(min_value=1, max_value=MAX_BIRDS)
    date = serializers.DateField(required=False, allow_null=True, default=None)
    note = serializers.CharField(max_length=500, allow_blank=True, required=False, default="")


class DailyReportSerializer(serializers.ModelSerializer):
    household = HouseholdShortSerializer(read_only=True)
    created_by = UserRefSerializer(read_only=True, allow_null=True)

    class Meta:
        model = DailyReport
        fields = (
            "id",
            "household",
            "date",
            "bird_count",
            "feed_kg",
            "eggs",
            "meat_kg",
            "deaths",
            "avg_weight_g",
            "note",
            "created_by",
            "created_at",
        )
        read_only_fields = fields


class DailyReportCreateSerializer(serializers.Serializer):
    date = serializers.DateField(required=False, allow_null=True, default=None)
    feed_kg = serializers.DecimalField(max_digits=10, decimal_places=2, min_value=0)
    eggs = serializers.IntegerField(min_value=0, max_value=MAX_BIRDS)
    meat_kg = serializers.DecimalField(
        max_digits=10, decimal_places=2, min_value=0, required=False, allow_null=True, default=None
    )
    deaths = serializers.IntegerField(min_value=0, max_value=MAX_BIRDS, required=False, default=0)
    avg_weight_g = serializers.DecimalField(
        max_digits=8, decimal_places=2, min_value=0, required=False, allow_null=True, default=None
    )
    note = serializers.CharField(max_length=500, allow_blank=True, required=False, default="")
    batch = PrimaryKeyField(
        queryset=PoultryBatch.objects.all(), required=False, allow_null=True, default=None
    )
    household = PrimaryKeyField(
        queryset=Household.objects.select_related("owner"), required=False, allow_null=True, default=None
    )

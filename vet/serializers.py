"""DRF serializers of the vet app (API_CONTRACT_V3 §8).

Read serializers expect the ``selectors`` querysets (``select_related`` done) and a ``files`` map in the context
(``{object_id: [Attachment]}`` from :func:`core.files.files_by_object`, one query per list) so list endpoints stay
within the §17.3 budget. ``allowed_actions`` depends on the requesting user (context ``request``).
"""

from __future__ import annotations

from typing import Any

from django.utils import timezone
from drf_spectacular.utils import extend_schema_field
from rest_framework import serializers

from accounts.models import User
from accounts.serializers import UserRefSerializer
from common.fields import PrimaryKeyField
from common.roles import Role, role_of
from core import files
from core.serializers import AttachmentSerializer
from farm.models import Household, PoultryBatch
from farm.serializers import BatchRefSerializer, HouseholdShortSerializer
from warehouse.models import Product

from .models import (
    MAX_LONG_TEXT,
    DiseaseRecord,
    DiseaseStatus,
    HealthStatus,
    RequestStatus,
    Treatment,
    Vaccination,
    VetRequest,
    VetStatus,
    VetVisit,
)

MSG_REQUIRED = "Bu maydon to‘ldirilishi shart."
MSG_MIN = "Kamida {n} ta belgi kiriting."
MSG_MAX = "Ko‘pi bilan {n} ta belgi kiritish mumkin."
MSG_POSITIVE = "0 dan katta butun son kiriting."
MSG_DATE = "Sana noto‘g‘ri formatda (YYYY-MM-DD)."
MAX_BIRDS = 10_000_000


def text_field(*, min_length: int = 0, max_length: int, required: bool = False, **kwargs) -> serializers.CharField:
    messages = {
        "max_length": MSG_MAX.format(n=max_length),
        "required": MSG_REQUIRED,
        "null": MSG_REQUIRED,
    }
    if min_length:
        messages["min_length"] = MSG_MIN.format(n=min_length)
        messages["blank"] = MSG_MIN.format(n=min_length)
    kwargs.setdefault("allow_blank", not min_length)
    return serializers.CharField(
        min_length=min_length or None,
        max_length=max_length,
        required=required,
        trim_whitespace=True,
        error_messages=messages,
        **kwargs,
    )


def count_field(**kwargs) -> serializers.IntegerField:
    return serializers.IntegerField(
        min_value=1,
        max_value=MAX_BIRDS,
        error_messages={"min_value": MSG_POSITIVE, "max_value": "Qiymat juda katta.", "invalid": MSG_POSITIVE},
        **kwargs,
    )


def quantity_field(**kwargs) -> serializers.DecimalField:
    message = "0 yoki undan katta son kiriting (verguldan keyin 2 ta raqam)."
    return serializers.DecimalField(
        max_digits=12,
        decimal_places=2,
        min_value=0,
        error_messages={
            "invalid": message,
            "min_value": message,
            "max_digits": "Qiymat juda katta.",
            "max_whole_digits": "Qiymat juda katta.",
            "max_decimal_places": message,
        },
        **kwargs,
    )


def date_field(**kwargs) -> serializers.DateField:
    return serializers.DateField(error_messages={"invalid": MSG_DATE, "required": MSG_REQUIRED}, **kwargs)


def household_field(**kwargs) -> PrimaryKeyField:
    return PrimaryKeyField(
        queryset=Household.objects.select_related("owner"),
        error_messages={"does_not_exist": "Xonadon topilmadi.", "required": MSG_REQUIRED, "null": MSG_REQUIRED},
        **kwargs,
    )


def batch_field(**kwargs) -> PrimaryKeyField:
    kwargs.setdefault("required", False)
    kwargs.setdefault("allow_null", True)
    return PrimaryKeyField(
        queryset=PoultryBatch.objects.all(), error_messages={"does_not_exist": "Partiya topilmadi."}, **kwargs
    )


def vet_user_field(**kwargs) -> PrimaryKeyField:
    kwargs.setdefault("required", False)
    kwargs.setdefault("allow_null", True)
    return PrimaryKeyField(
        queryset=User.objects.all(),
        error_messages={"does_not_exist": "Faqat faol veterinarni tanlash mumkin."},
        **kwargs,
    )


def product_field(**kwargs) -> PrimaryKeyField:
    kwargs.setdefault("required", False)
    kwargs.setdefault("allow_null", True)
    return PrimaryKeyField(
        queryset=Product.objects.all(), error_messages={"does_not_exist": "Mahsulot topilmadi."}, **kwargs
    )


def _inline_enum(base: type[serializers.Field], values: list[str]) -> type[serializers.Field]:
    """Field class documented as a string listing its values (no `enum` → no shared `StatusEnum` component:
    avoids the spectacular name collision of the many `status` fields of different modules)."""
    schema = {"type": "string", "description": " | ".join(values)}
    return extend_schema_field(schema)(type(f"Plain{base.__name__}", (base,), {}))


def status_out(choices) -> serializers.CharField:
    return _inline_enum(serializers.CharField, choices.values)(read_only=True)


def _choice(choices, **kwargs) -> serializers.ChoiceField:
    return _inline_enum(serializers.ChoiceField, [value for value, _ in choices])(
        choices=choices,
        error_messages={"invalid_choice": "“{input}” ruxsat etilgan qiymat emas.", "required": MSG_REQUIRED},
        **kwargs,
    )


class _StrictMixin:
    """Unknown keys in a PATCH body → ``400 {"<key>": ["Bu maydonni o‘zgartirib bo‘lmaydi."]}``."""

    def validate(self, attrs: dict) -> dict:
        initial = getattr(self, "initial_data", None)
        if isinstance(initial, dict):
            unknown = sorted(set(initial) - set(self.fields))
            if unknown:
                raise serializers.ValidationError({unknown[0]: ["Bu maydonni o‘zgartirib bo‘lmaydi."]})
        return super().validate(attrs)


# --- Read side -----------------------------------------------------------------------------------


class VetProductRefSerializer(serializers.ModelSerializer):
    class Meta:
        model = Product
        fields = ("id", "name", "unit", "category")


class _VetReadSerializer(serializers.ModelSerializer):
    """Common fields: household, vet, created_by, files (from the context map), allowed_actions."""

    household = HouseholdShortSerializer(read_only=True)
    vet = UserRefSerializer(read_only=True, allow_null=True)
    created_by = UserRefSerializer(read_only=True, allow_null=True)
    code = serializers.CharField(read_only=True)
    status_display = serializers.CharField(source="get_status_display", read_only=True)
    files = serializers.SerializerMethodField()
    allowed_actions = serializers.SerializerMethodField()

    def _user(self) -> User | None:
        request = self.context.get("request")
        return getattr(request, "user", None) if request is not None else self.context.get("user")

    def _role(self) -> str | None:
        return role_of(self._user())

    def get_files(self, obj) -> list[dict[str, Any]]:
        file_map = self.context.get("files")
        rows = file_map.get(obj.pk, []) if file_map is not None else files.files_for(obj)
        return AttachmentSerializer(rows, many=True).data

    def get_allowed_actions(self, obj) -> list[str]:
        return []


def _is_overdue(obj) -> bool:
    return obj.status == VetStatus.PLANNED and obj.planned_date < timezone.localdate()


class RequestRefSerializer(serializers.ModelSerializer):
    code = serializers.CharField(read_only=True)

    class Meta:
        model = VetRequest
        fields = ("id", "code")


class VetVisitSerializer(_VetReadSerializer):
    status = status_out(VetStatus)
    batch = BatchRefSerializer(read_only=True, allow_null=True)
    request = RequestRefSerializer(read_only=True, allow_null=True)
    is_overdue = serializers.SerializerMethodField()
    health_status = serializers.SerializerMethodField()
    health_status_display = serializers.SerializerMethodField()

    class Meta:
        model = VetVisit
        fields = (
            "id",
            "code",
            "household",
            "batch",
            "vet",
            "planned_date",
            "purpose",
            "status",
            "status_display",
            "is_overdue",
            "done_at",
            "birds_checked",
            "health_status",
            "health_status_display",
            "findings",
            "conclusion",
            "recommendations",
            "request",
            "cancel_reason",
            "files",
            "created_by",
            "created_at",
            "allowed_actions",
        )
        read_only_fields = fields

    def get_is_overdue(self, obj: VetVisit) -> bool:
        return _is_overdue(obj)

    def get_health_status(self, obj: VetVisit) -> str | None:
        return obj.health_status or None

    def get_health_status_display(self, obj: VetVisit) -> str | None:
        return obj.get_health_status_display() if obj.health_status else None

    def get_allowed_actions(self, obj: VetVisit) -> list[str]:
        user, role = self._user(), self._role()
        if obj.status != VetStatus.PLANNED or role not in (Role.ADMIN, Role.VET):
            return []
        actions = ["update", "cancel"]
        if role == Role.VET and (obj.vet_id is None or obj.vet_id == user.pk):
            actions.insert(0, "complete")
        return actions


class VaccinationSerializer(_VetReadSerializer):
    status = status_out(VetStatus)
    batch = BatchRefSerializer(read_only=True, allow_null=True)
    product = VetProductRefSerializer(read_only=True, allow_null=True)
    is_overdue = serializers.SerializerMethodField()
    quantity_used = serializers.DecimalField(
        max_digits=12, decimal_places=2, coerce_to_string=False, read_only=True, allow_null=True
    )
    stock_op_id = serializers.SerializerMethodField()

    class Meta:
        model = Vaccination
        fields = (
            "id",
            "code",
            "household",
            "batch",
            "vet",
            "product",
            "vaccine_name",
            "dose",
            "planned_date",
            "birds_planned",
            "status",
            "status_display",
            "is_overdue",
            "done_date",
            "birds_vaccinated",
            "quantity_used",
            "result",
            "note",
            "stock_op_id",
            "cancel_reason",
            "files",
            "created_by",
            "created_at",
            "allowed_actions",
        )
        read_only_fields = fields

    def get_is_overdue(self, obj: Vaccination) -> bool:
        return _is_overdue(obj)

    def get_stock_op_id(self, obj: Vaccination) -> str | None:
        return obj.stock_transaction.op_id if obj.stock_transaction_id else None

    def get_allowed_actions(self, obj: Vaccination) -> list[str]:
        user, role = self._user(), self._role()
        if obj.status != VetStatus.PLANNED or role not in (Role.ADMIN, Role.VET):
            return []
        actions = ["update", "cancel"]
        if role == Role.VET and (obj.vet_id is None or obj.vet_id == user.pk):
            actions.insert(0, "complete")
        return actions


class TreatmentShortSerializer(serializers.ModelSerializer):
    code = serializers.CharField(read_only=True)
    vet = UserRefSerializer(read_only=True, allow_null=True)
    is_active = serializers.BooleanField(read_only=True)

    class Meta:
        model = Treatment
        fields = ("id", "code", "drug_name", "dosage", "start_date", "end_date", "instructions", "vet", "is_active")
        read_only_fields = fields


class DiseaseRefSerializer(serializers.ModelSerializer):
    status = status_out(DiseaseStatus)
    code = serializers.CharField(read_only=True)
    status_display = serializers.CharField(source="get_status_display", read_only=True)

    class Meta:
        model = DiseaseRecord
        fields = ("id", "code", "disease_name", "status", "status_display")


class VisitRefSerializer(serializers.ModelSerializer):
    code = serializers.CharField(read_only=True)

    class Meta:
        model = VetVisit
        fields = ("id", "code")


class DiseaseRecordSerializer(_VetReadSerializer):
    status = status_out(DiseaseStatus)
    batch = BatchRefSerializer(read_only=True, allow_null=True)
    visit = VisitRefSerializer(read_only=True, allow_null=True)
    deaths_since_diagnosis = serializers.IntegerField(read_only=True, default=0)
    treatments = serializers.SerializerMethodField()

    class Meta:
        model = DiseaseRecord
        fields = (
            "id",
            "code",
            "household",
            "batch",
            "vet",
            "visit",
            "disease_name",
            "diagnosed_date",
            "cases",
            "symptoms",
            "status",
            "status_display",
            "resolved_date",
            "notes",
            "deaths_since_diagnosis",
            "treatments",
            "files",
            "created_by",
            "created_at",
            "allowed_actions",
        )
        read_only_fields = fields

    def get_treatments(self, obj: DiseaseRecord) -> list[dict[str, Any]] | None:
        """Only on the detail (``with_treatments`` in the context); lists stay within the query budget."""
        if not self.context.get("with_treatments"):
            return None
        rows = obj.treatments.select_related("vet").order_by("-start_date", "-id")
        return TreatmentShortSerializer(rows, many=True).data

    def get_allowed_actions(self, obj: DiseaseRecord) -> list[str]:
        role = self._role()
        if obj.status == DiseaseStatus.RESOLVED or role not in (Role.ADMIN, Role.VET):
            return []
        return ["update", "treat"] if role == Role.VET else ["update"]


class TreatmentSerializer(_VetReadSerializer):
    batch = BatchRefSerializer(read_only=True, allow_null=True)
    disease = DiseaseRefSerializer(read_only=True, allow_null=True)
    product = VetProductRefSerializer(read_only=True, allow_null=True)
    quantity_used = serializers.DecimalField(
        max_digits=12, decimal_places=2, coerce_to_string=False, read_only=True, allow_null=True
    )
    is_active = serializers.BooleanField(read_only=True)
    stock_op_id = serializers.SerializerMethodField()

    class Meta:
        model = Treatment
        fields = (
            "id",
            "code",
            "household",
            "batch",
            "disease",
            "vet",
            "product",
            "drug_name",
            "dosage",
            "start_date",
            "end_date",
            "is_active",
            "quantity_used",
            "instructions",
            "stock_op_id",
            "files",
            "created_by",
            "created_at",
            "allowed_actions",
        )
        read_only_fields = fields

    def get_stock_op_id(self, obj: Treatment) -> str | None:
        return obj.stock_transaction.op_id if obj.stock_transaction_id else None

    def get_allowed_actions(self, obj: Treatment) -> list[str]:
        return ["update"] if self._role() == Role.VET else []


class VetRequestSerializer(_VetReadSerializer):
    status = status_out(RequestStatus)
    closed_by = UserRefSerializer(read_only=True, allow_null=True)

    class Meta:
        model = VetRequest
        fields = (
            "id",
            "code",
            "household",
            "text",
            "status",
            "status_display",
            "vet",
            "response",
            "files",
            "created_by",
            "created_at",
            "closed_at",
            "closed_by",
            "allowed_actions",
        )
        read_only_fields = fields

    def get_allowed_actions(self, obj: VetRequest) -> list[str]:
        role = self._role()
        if obj.status == RequestStatus.CLOSED:
            return []
        if role == Role.VET:
            return (["take"] if obj.status == RequestStatus.NEW else []) + ["plan_visit", "close"]
        if role == Role.ADMIN:
            return ["plan_visit", "close"]
        if role == Role.OWNER and obj.status == RequestStatus.NEW:
            return ["close"]
        return []


# --- Write side ----------------------------------------------------------------------------------


class VisitPlanSerializer(serializers.Serializer):
    household = household_field()
    planned_date = date_field()
    purpose = text_field(min_length=3, max_length=500, required=True)
    batch = batch_field()
    vet = vet_user_field()
    request = PrimaryKeyField(
        queryset=VetRequest.objects.all(),
        required=False,
        allow_null=True,
        error_messages={"does_not_exist": "Murojaat topilmadi."},
    )


class RequestVisitPlanSerializer(serializers.Serializer):
    """``POST /api/vet/requests/{id}/plan-visit`` — §8.1 body without ``household``."""

    planned_date = date_field()
    purpose = text_field(min_length=3, max_length=500, required=True)
    batch = batch_field()
    vet = vet_user_field()


class VisitUpdateSerializer(_StrictMixin, serializers.Serializer):
    planned_date = date_field(required=False)
    purpose = text_field(min_length=3, max_length=500)
    batch = batch_field()
    vet = vet_user_field()


class VisitCompleteSerializer(serializers.Serializer):
    done_at = serializers.DateTimeField(required=False, allow_null=True)
    health_status = _choice(HealthStatus.choices)
    birds_checked = count_field(required=False, allow_null=True)
    findings = text_field(max_length=MAX_LONG_TEXT)
    conclusion = text_field(min_length=3, max_length=MAX_LONG_TEXT, required=True)
    recommendations = text_field(max_length=MAX_LONG_TEXT)
    file_ids = files.FileIdsField()
    update_batch_health = serializers.BooleanField(required=False, default=False)


class VetReasonSerializer(serializers.Serializer):
    reason = text_field(min_length=3, max_length=500, required=True)


class VaccinationPlanSerializer(serializers.Serializer):
    household = household_field(required=False, allow_null=True)
    household_ids = serializers.ListField(
        child=serializers.IntegerField(min_value=1, max_value=2**63 - 1),
        required=False,
        min_length=1,
        max_length=200,
        error_messages={
            "min_length": "Kamida bitta xonadon tanlang.",
            "max_length": "Bir martada ko‘pi bilan 200 ta xonadon tanlash mumkin.",
        },
    )
    planned_date = date_field()
    vaccine_name = text_field(max_length=200)
    product = product_field()
    dose = text_field(max_length=100)
    birds_planned = count_field(required=False, allow_null=True)
    batch = batch_field()
    vet = vet_user_field()
    note = text_field(max_length=500)

    def validate(self, attrs: dict) -> dict:
        has_one = attrs.get("household") is not None
        has_many = bool(attrs.get("household_ids"))
        if has_one == has_many:
            raise serializers.ValidationError({"household": ["Xonadonni yoki xonadonlar ro‘yxatini tanlang."]})
        return attrs


class VaccinationUpdateSerializer(_StrictMixin, serializers.Serializer):
    planned_date = date_field(required=False)
    dose = text_field(max_length=100)
    birds_planned = count_field(required=False, allow_null=True)
    vet = vet_user_field()
    note = text_field(max_length=500)
    vaccine_name = text_field(max_length=200)
    product = product_field()
    batch = batch_field()


class VaccinationCompleteSerializer(serializers.Serializer):
    done_date = date_field()
    birds_vaccinated = count_field()
    quantity_used = quantity_field(required=False, allow_null=True)
    result = text_field(min_length=2, max_length=2000, required=True)
    note = text_field(max_length=500)
    file_ids = files.FileIdsField()


class DiseaseCreateSerializer(serializers.Serializer):
    household = household_field()
    disease_name = text_field(min_length=2, max_length=200, required=True)
    diagnosed_date = date_field()
    cases = count_field()
    symptoms = text_field(max_length=2000)
    batch = batch_field()
    visit = PrimaryKeyField(
        queryset=VetVisit.objects.all(),
        required=False,
        allow_null=True,
        error_messages={"does_not_exist": "Tekshiruv topilmadi."},
    )
    vet = vet_user_field()
    notes = text_field(max_length=2000)
    file_ids = files.FileIdsField()


class DiseaseUpdateSerializer(_StrictMixin, serializers.Serializer):
    status = _choice(DiseaseStatus.choices, required=False)
    resolved_date = date_field(required=False, allow_null=True)
    cases = count_field(required=False)
    notes = text_field(max_length=2000)
    symptoms = text_field(max_length=2000)


class TreatmentCreateSerializer(serializers.Serializer):
    household = household_field()
    disease = PrimaryKeyField(
        queryset=DiseaseRecord.objects.all(),
        required=False,
        allow_null=True,
        error_messages={"does_not_exist": "Kasallik yozuvi topilmadi."},
    )
    batch = batch_field()
    product = product_field()
    drug_name = text_field(max_length=200)
    dosage = text_field(max_length=200)
    start_date = date_field()
    end_date = date_field(required=False, allow_null=True)
    quantity_used = quantity_field(required=False, allow_null=True)
    instructions = text_field(max_length=MAX_LONG_TEXT)
    file_ids = files.FileIdsField()


class TreatmentUpdateSerializer(_StrictMixin, serializers.Serializer):
    end_date = date_field(required=False, allow_null=True)
    instructions = text_field(max_length=MAX_LONG_TEXT)


class VetRequestCreateSerializer(serializers.Serializer):
    text = text_field(min_length=5, max_length=2000, required=True)
    file_ids = files.FileIdsField(max_length=5, error_messages={"max_length": "Ko‘pi bilan 5 ta rasm biriktirish mumkin."})


class RequestTakeSerializer(serializers.Serializer):
    response = text_field(max_length=MAX_LONG_TEXT)


class RequestCloseSerializer(serializers.Serializer):
    response = text_field(max_length=MAX_LONG_TEXT)

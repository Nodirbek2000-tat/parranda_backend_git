from __future__ import annotations

from decimal import Decimal

from rest_framework import serializers

from accounts.serializers import UserRefSerializer, validate_phone

from .models import Attachment, SystemSettings


def _int_range(low: int, high: int, **kwargs) -> serializers.IntegerField:
    message = f"{low} dan {high} gacha butun son kiriting."
    return serializers.IntegerField(
        min_value=low,
        max_value=high,
        error_messages={"invalid": message, "min_value": message, "max_value": message, "max_string_length": message},
        **kwargs,
    )


class SystemSettingsSerializer(serializers.ModelSerializer):
    updated_by = UserRefSerializer(read_only=True, allow_null=True)

    class Meta:
        model = SystemSettings
        fields = (
            "company_name",
            "short_name",
            "phone",
            "address",
            "vaccination_reminder_days",
            "expiry_reminder_days",
            "mortality_alert_threshold_pct",
            "problem_report_days",
            "low_stock_alert_enabled",
            "delivery_proof_required",
            "reminder_hour",
            "last_reminders_date",
            "updated_at",
            "updated_by",
        )
        read_only_fields = fields


_PCT_MESSAGE = "0 dan 100 gacha son kiriting (verguldan keyin 1 ta raqam)."


class SystemSettingsUpdateSerializer(serializers.Serializer):
    company_name = serializers.CharField(
        max_length=200,
        min_length=2,
        required=False,
        error_messages={"min_length": "Kamida 2 ta belgi kiriting.", "blank": "Kamida 2 ta belgi kiriting."},
    )
    short_name = serializers.CharField(
        max_length=20, min_length=1, required=False, error_messages={"blank": "Kamida 1 ta belgi kiriting."}
    )
    phone = serializers.CharField(max_length=32, allow_blank=True, required=False, validators=[validate_phone])
    address = serializers.CharField(max_length=255, allow_blank=True, required=False)
    vaccination_reminder_days = _int_range(0, 30, required=False)
    expiry_reminder_days = _int_range(0, 365, required=False)
    mortality_alert_threshold_pct = serializers.DecimalField(
        max_digits=4,
        decimal_places=1,
        min_value=Decimal("0"),
        max_value=Decimal("100"),
        required=False,
        error_messages={
            "invalid": _PCT_MESSAGE,
            "min_value": _PCT_MESSAGE,
            "max_value": _PCT_MESSAGE,
            "max_digits": _PCT_MESSAGE,
            "max_decimal_places": _PCT_MESSAGE,
            "max_whole_digits": _PCT_MESSAGE,
        },
    )
    problem_report_days = _int_range(1, 30, required=False)
    low_stock_alert_enabled = serializers.BooleanField(required=False)
    delivery_proof_required = serializers.BooleanField(required=False)
    reminder_hour = _int_range(0, 23, required=False)

    def validate(self, attrs: dict) -> dict:
        unknown = set(self.initial_data) - set(self.fields) if isinstance(self.initial_data, dict) else set()
        if unknown:
            name = sorted(unknown)[0]
            raise serializers.ValidationError({name: ["Bu maydonni o‘zgartirib bo‘lmaydi."]})
        return attrs


class AttachmentSerializer(serializers.ModelSerializer):
    """The ``File`` object (API_CONTRACT_V3 §4.2). ``url`` is an API path, never a storage URL."""

    name = serializers.CharField(source="original_name", read_only=True)
    url = serializers.CharField(read_only=True)

    class Meta:
        model = Attachment
        fields = ("id", "name", "content_type", "size", "purpose", "url", "created_at")
        read_only_fields = fields

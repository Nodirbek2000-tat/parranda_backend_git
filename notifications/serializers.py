"""Serializers of the notification monitor, settings and the bell (API_CONTRACT_V3 §9.3, §9.5, §9.6)."""

from __future__ import annotations

from rest_framework import serializers

from accounts.serializers import UserRefSerializer
from common.roles import NOTIFIABLE_ROLES, STAFF_ROLE_CHOICES

from . import registry
from .models import Notification


class RecipientSerializer(serializers.Serializer):
    id = serializers.IntegerField()
    full_name = serializers.CharField()
    role = serializers.CharField(source="effective_role")


class NotificationMonitorSerializer(serializers.ModelSerializer):
    recipient = RecipientSerializer(read_only=True)
    event_display = serializers.SerializerMethodField()
    status_display = serializers.CharField(source="get_status_display", read_only=True)
    telegram_linked = serializers.BooleanField(source="recipient.telegram_linked", read_only=True)

    class Meta:
        model = Notification
        fields = (
            "id",
            "recipient",
            "event",
            "event_display",
            "title",
            "related_op",
            "status",
            "status_display",
            "attempts",
            "last_error",
            "telegram_linked",
            "button_path",
            "created_at",
            "sent_at",
            "read_at",
        )
        read_only_fields = fields

    def get_event_display(self, row: Notification) -> str:
        return registry.display(row.event)


class NotificationMonitorDetailSerializer(NotificationMonitorSerializer):
    class Meta(NotificationMonitorSerializer.Meta):
        fields = (*NotificationMonitorSerializer.Meta.fields, "text", "body")
        read_only_fields = fields


class MyNotificationSerializer(serializers.ModelSerializer):
    event_display = serializers.SerializerMethodField()

    class Meta:
        model = Notification
        fields = ("id", "event", "event_display", "title", "body", "button_path", "related_op", "created_at", "read_at")
        read_only_fields = fields

    def get_event_display(self, row: Notification) -> str:
        return registry.display(row.event)


class BotInboxSerializer(serializers.ModelSerializer):
    class Meta:
        model = Notification
        fields = ("id", "title", "body", "button_path", "created_at", "read_at")
        read_only_fields = fields


class NotificationSettingSerializer(serializers.Serializer):
    """One registry event + its stored switches (``None`` row = defaults)."""

    event = serializers.CharField()
    display = serializers.CharField()
    description = serializers.CharField()
    enabled = serializers.BooleanField()
    telegram_enabled = serializers.BooleanField()
    default_roles = serializers.ListField(child=serializers.CharField())
    roles = serializers.ListField(child=serializers.CharField())
    roles_overridden = serializers.BooleanField()
    contextual = serializers.ListField(child=serializers.CharField())
    contextual_enabled = serializers.BooleanField()
    updated_by = UserRefSerializer(allow_null=True)
    updated_at = serializers.DateTimeField(allow_null=True)

    @staticmethod
    def build(definition: registry.EventDef, row) -> dict:
        overridden = row is not None and row.roles is not None
        return {
            "event": definition.key,
            "display": definition.display,
            "description": definition.description,
            "enabled": row.enabled if row is not None else True,
            "telegram_enabled": row.telegram_enabled if row is not None else True,
            "default_roles": list(definition.default_roles),
            "roles": list(row.roles) if overridden else list(definition.default_roles),
            "roles_overridden": overridden,
            "contextual": list(definition.contextual),
            "contextual_enabled": row.contextual_enabled if row is not None else True,
            "updated_by": row.updated_by if row is not None else None,
            "updated_at": row.updated_at if row is not None else None,
        }


class NotificationSettingUpdateSerializer(serializers.Serializer):
    enabled = serializers.BooleanField(required=False)
    telegram_enabled = serializers.BooleanField(required=False)
    roles = serializers.ListField(
        child=serializers.ChoiceField(choices=STAFF_ROLE_CHOICES), required=False, allow_null=True, max_length=4
    )
    contextual_enabled = serializers.BooleanField(required=False)

    def validate_roles(self, value):
        if value is None:
            return None
        return [role for role in NOTIFIABLE_ROLES if role in set(value)]  # canonical order, no duplicates

    def to_internal_value(self, data):
        # ``roles: ["OWNER"]`` → {"roles": ["“OWNER” ruxsat etilgan qiymat emas."]} (flat list, V3 §9.3).
        try:
            return super().to_internal_value(data)
        except serializers.ValidationError as exc:
            detail = exc.detail
            if isinstance(detail, dict) and isinstance(detail.get("roles"), dict):
                messages = [msg for msgs in detail["roles"].values() for msg in msgs]
                detail["roles"] = messages[:1]
            raise serializers.ValidationError(detail) from None


class RetryFailedSerializer(serializers.Serializer):
    event = serializers.CharField(required=False, allow_blank=True, default="", max_length=50)
    date_from = serializers.DateField(required=False, allow_null=True, default=None)
    date_to = serializers.DateField(required=False, allow_null=True, default=None)

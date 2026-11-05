from __future__ import annotations

from drf_spectacular.utils import extend_schema_field
from rest_framework import serializers

from accounts.serializers import UserRefSerializer

from .models import AuditLog


@extend_schema_field(
    {"type": "object", "nullable": True, "additionalProperties": True, "description": "Maydon → qiymat"}
)
class JsonObjectField(serializers.JSONField):
    pass


class AuditLogSerializer(serializers.ModelSerializer):
    actor = UserRefSerializer(read_only=True, allow_null=True)
    module_display = serializers.CharField(source="get_module_display", read_only=True)
    action_display = serializers.CharField(source="get_action_display", read_only=True)
    before = JsonObjectField(read_only=True, allow_null=True)
    after = JsonObjectField(read_only=True, allow_null=True)
    ip = serializers.IPAddressField(read_only=True, allow_null=True)
    status_display = serializers.CharField(source="get_status_display", read_only=True)

    class Meta:
        model = AuditLog
        fields = (
            "id",
            "op_id",
            "actor",
            "actor_name",
            "role",
            "source",
            "module",
            "module_display",
            "action",
            "action_display",
            "object_type",
            "object_id",
            "object_repr",
            "before",
            "after",
            "ip",
            "household_ref",
            "product_ref",
            "status",
            "status_display",
            "created_at",
        )
        read_only_fields = fields


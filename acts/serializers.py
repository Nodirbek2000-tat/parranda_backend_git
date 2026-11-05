from __future__ import annotations

from drf_spectacular.utils import extend_schema_field, inline_serializer
from rest_framework import serializers

from accounts.serializers import UserRefSerializer
from common.roles import Role
from farm.serializers import HouseholdShortSerializer

from .models import Act, ActKind

_ROW = serializers.ListField(child=serializers.CharField())

SNAPSHOT_SCHEMA = inline_serializer(
    "ActSnapshot",
    {
        "code": serializers.CharField(),
        "title": serializers.CharField(),
        "kind": serializers.ChoiceField(choices=ActKind.choices),
        "role": serializers.CharField(allow_null=True),
        "role_display": serializers.CharField(allow_null=True),
        "company": serializers.CharField(),
        "date": serializers.CharField(),
        "period": serializers.CharField(),
        "household": inline_serializer(
            "ActHousehold",
            {key: serializers.CharField() for key in ("code", "name", "owner", "district", "address")},
            allow_null=True,
        ),
        "responsible": serializers.CharField(),
        "body": serializers.CharField(),
        "summary": inline_serializer("ActLabelValue", {"label": serializers.CharField(),
                                                       "value": serializers.CharField()}, many=True),
        "sections": inline_serializer(
            "ActSection",
            {"title": serializers.CharField(), "columns": serializers.ListField(child=serializers.CharField()),
             "rows": serializers.ListField(child=_ROW)},
            many=True,
        ),
        "tasks": inline_serializer(
            "ActTask",
            {"code": serializers.CharField(), "title": serializers.CharField(), "household": serializers.CharField(),
             "approved_at": serializers.CharField(),
             "values": inline_serializer("ActTaskValue", {"label": serializers.CharField(),
                                                          "value": serializers.CharField()}, many=True)},
            many=True,
        ),
        "photos": inline_serializer("ActPhoto", {"file_id": serializers.IntegerField(),
                                                 "label": serializers.CharField()}, many=True),
        "approvals": inline_serializer("ActApproval", {"label": serializers.CharField(), "name": serializers.CharField(),
                                                       "at": serializers.CharField()}, many=True),
        "signatures": serializers.ListField(child=serializers.CharField()),
    },
)


class ActRequestSerializer(serializers.Serializer):
    """Body of ``POST /api/acts`` and query of ``GET /api/acts/preview``."""

    kind = serializers.ChoiceField(choices=ActKind.choices)
    role = serializers.ChoiceField(choices=Role.choices, required=False, allow_blank=True)
    user_id = serializers.IntegerField(required=False, allow_null=True, min_value=1, max_value=2**63 - 1)
    household_id = serializers.IntegerField(required=False, allow_null=True, min_value=1, max_value=2**63 - 1)
    task_id = serializers.IntegerField(required=False, allow_null=True, min_value=1, max_value=2**63 - 1)
    date_from = serializers.DateField(required=False, allow_null=True)
    date_to = serializers.DateField(required=False, allow_null=True)

    def validate(self, attrs: dict) -> dict:
        kind = attrs["kind"]
        if kind == ActKind.ROLE and not attrs.get("role"):
            raise serializers.ValidationError({"role": ["Bu maydon to‘ldirilishi shart."]})
        if kind == ActKind.TASK and not attrs.get("task_id"):
            raise serializers.ValidationError({"task_id": ["Bu maydon to‘ldirilishi shart."]})
        return attrs


class ActSerializer(serializers.ModelSerializer):
    """``Act`` of the frontend; the list omits ``snapshot`` (``ActDetailSerializer`` adds it)."""

    code = serializers.CharField(read_only=True)
    kind_display = serializers.CharField(source="get_kind_display", read_only=True)
    role = serializers.SerializerMethodField()
    role_display = serializers.SerializerMethodField()
    user = UserRefSerializer(read_only=True, allow_null=True)
    household = HouseholdShortSerializer(read_only=True, allow_null=True)
    task_id = serializers.IntegerField(read_only=True, allow_null=True)
    created_by_name = serializers.SerializerMethodField()

    class Meta:
        model = Act
        fields = (
            "id",
            "code",
            "kind",
            "kind_display",
            "role",
            "role_display",
            "user",
            "household",
            "task_id",
            "date_from",
            "date_to",
            "created_by_name",
            "created_at",
        )
        read_only_fields = fields

    @extend_schema_field(serializers.CharField(allow_null=True))
    def get_role(self, act: Act) -> str | None:
        return act.role or None

    @extend_schema_field(serializers.CharField(allow_null=True))
    def get_role_display(self, act: Act) -> str | None:
        return act.get_role_display() if act.role else None

    @extend_schema_field(serializers.CharField(allow_null=True))
    def get_created_by_name(self, act: Act) -> str | None:
        return act.created_by.full_name if act.created_by_id else None


class ActDetailSerializer(ActSerializer):
    snapshot = serializers.SerializerMethodField()

    class Meta(ActSerializer.Meta):
        fields = (*ActSerializer.Meta.fields, "snapshot")
        read_only_fields = fields

    @extend_schema_field(SNAPSHOT_SCHEMA)
    def get_snapshot(self, act: Act) -> dict:
        return act.snapshot

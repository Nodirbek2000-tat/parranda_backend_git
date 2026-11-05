"""Report export endpoints (API_CONTRACT_V3 §11.1) and the region options of the filter bar."""

from __future__ import annotations

from django.http import HttpResponse
from django.utils import timezone
from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import OpenApiResponse, extend_schema, inline_serializer
from rest_framework import serializers
from rest_framework.negotiation import DefaultContentNegotiation
from rest_framework.exceptions import NotFound, PermissionDenied, ValidationError
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.throttling import ScopedRateThrottle
from rest_framework.views import APIView

from common.permissions import IsAuthenticatedRole, IsRoleIn, MSG_NO_PERMISSION
from common.roles import Role, role_of
from common.schema import ERROR_400, query as query_param
from farm.models import Household, PoultryType

from . import params as report_params
from . import services
from .registry import BY_KEY, FORMATS, available_for

MSG_UNKNOWN = "Hisobot topilmadi."

ReportDefinitionSerializer = inline_serializer(
    "ReportDefinition",
    {
        "key": serializers.CharField(),
        "title": serializers.CharField(),
        "description": serializers.CharField(),
        "formats": serializers.ListField(child=serializers.CharField()),
        "filters": serializers.ListField(child=serializers.CharField()),
        "max_days": serializers.IntegerField(),
    },
    many=True,
)

ReportPreviewSerializer = inline_serializer(
    "ReportPreview",
    {
        "key": serializers.CharField(),
        "title": serializers.CharField(),
        "period": serializers.DictField(),
        "filters": serializers.DictField(),
        "generated_at": serializers.DateTimeField(),
        "columns": serializers.ListField(child=serializers.DictField()),
        "rows": serializers.ListField(child=serializers.DictField()),
        "totals": serializers.DictField(),
        "summary": serializers.ListField(child=serializers.DictField()),
        "row_count": serializers.IntegerField(),
        "truncated": serializers.BooleanField(),
    },
)


class ReportListView(APIView):
    """``GET /api/reports`` — reports available to the role (any role)."""

    permission_classes = [IsAuthenticatedRole]

    @extend_schema(responses=ReportDefinitionSerializer)
    def get(self, request: Request) -> Response:
        return Response([report.as_dict() for report in available_for(role_of(request.user))])


class ReportThrottle(ScopedRateThrottle):
    """``reports`` scope (30/min per user) for file exports only; the JSON preview is not throttled."""

    def allow_request(self, request, view) -> bool:
        if (request.query_params.get("format") or "json").strip() == "json":
            return True
        return super().allow_request(request, view)


class FormatParamNegotiation(DefaultContentNegotiation):
    """``?format=xlsx|pdf`` is the report format, not DRF's renderer override (which would answer 404)."""

    def select_renderer(self, request, renderers, format_suffix=None):
        return renderers[0], renderers[0].media_type


class ReportDetailView(APIView):
    """``GET /api/reports/{key}?format=json|xlsx|pdf&date_from=&date_to=&region=&district=&household=&poultry_type=``."""

    permission_classes = [IsAuthenticatedRole]
    throttle_classes = [ReportThrottle]
    throttle_scope = "reports"
    content_negotiation_class = FormatParamNegotiation

    @extend_schema(
        parameters=[
            query_param("format", enum=list(FORMATS), description="json (default), xlsx yoki pdf"),
            query_param("date_from", OpenApiTypes.DATE),
            query_param("date_to", OpenApiTypes.DATE),
            query_param("region"),
            query_param("district"),
            query_param("household", int),
            query_param("poultry_type", enum=PoultryType.values),
        ],
        responses={
            200: ReportPreviewSerializer,
            (200, "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"): OpenApiTypes.BINARY,
            (200, "application/pdf"): OpenApiTypes.BINARY,
            400: ERROR_400,
            403: OpenApiResponse(description="Hisobot bu rol uchun emas"),
            404: OpenApiResponse(description="Noma’lum hisobot yoki boshqa xonadon"),
        },
    )
    def get(self, request: Request, key: str) -> HttpResponse | Response:
        report = BY_KEY.get(key)
        if report is None:
            raise NotFound(MSG_UNKNOWN)
        if role_of(request.user) not in report.roles:
            raise PermissionDenied(MSG_NO_PERMISSION)
        fmt = (request.query_params.get("format") or "json").strip()
        if fmt not in FORMATS:
            raise ValidationError({"format": [f"“{fmt}” ruxsat etilgan qiymat emas."]})
        params = report_params.resolve(
            request.query_params,
            request.user,
            timezone.localdate(),
            monthly=report.monthly,
            max_days=report.max_days,
        )
        params = report_params.restrict(params, report.supported())
        if fmt == "json":
            return Response(services.preview(report, params))
        rendered = services.export(report, params, fmt)
        response = HttpResponse(rendered.content, content_type=rendered.content_type)
        response["Content-Disposition"] = rendered.disposition
        response["Cache-Control"] = "no-store"
        response["X-Content-Type-Options"] = "nosniff"
        return response


RegionSerializer = inline_serializer(
    "RegionOption",
    {"region": serializers.CharField(), "districts": serializers.ListField(child=serializers.CharField())},
    many=True,
)


class HouseholdRegionsView(APIView):
    """``GET /api/households/regions`` (A, W, V) → ``[{"region", "districts": [...]}]`` (V3 §11.1)."""

    permission_classes = [IsRoleIn(Role.ADMIN, Role.WAREHOUSE, Role.VET)]

    @extend_schema(responses=RegionSerializer)
    def get(self, request: Request) -> Response:
        pairs = (
            Household.objects.order_by("region", "district")
            .values_list("region", "district")
            .distinct()
        )
        grouped: dict[str, list[str]] = {}
        for region, district in pairs:
            districts = grouped.setdefault(region, [])
            if district and district not in districts:
                districts.append(district)
        return Response([{"region": region, "districts": districts} for region, districts in grouped.items()])

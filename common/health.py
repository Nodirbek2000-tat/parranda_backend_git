"""``GET /api/health`` and the OpenAPI schema / Swagger UI views (API_CONTRACT_V2 §3)."""

from __future__ import annotations

import logging

from django.conf import settings
from django.db import connection
from django.utils import timezone
from drf_spectacular.renderers import OpenApiJsonRenderer, OpenApiJsonRenderer2, OpenApiYamlRenderer
from drf_spectacular.utils import OpenApiResponse, extend_schema, inline_serializer
from drf_spectacular.views import SpectacularAPIView, SpectacularSwaggerView
from rest_framework import serializers
from rest_framework.authentication import SessionAuthentication
from rest_framework.permissions import AllowAny, BasePermission
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView

from accounts.authentication import RevocableJWTAuthentication

from .roles import Role, role_of

logger = logging.getLogger("nnp.health")

HealthSerializer = inline_serializer(
    "Health",
    fields={
        "status": serializers.ChoiceField(choices=[("ok", "ok"), ("error", "error")]),
        "db": serializers.ChoiceField(choices=[("ok", "ok"), ("error", "error")]),
        "time": serializers.DateTimeField(),
    },
)


def database_ok() -> bool:
    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT 1")
            cursor.fetchone()
    except Exception:  # any driver error means "database unavailable"
        logger.exception("Health check: database is unavailable")
        return False
    return True


class HealthView(APIView):
    """Public liveness/readiness probe: ``200`` when the database answers, ``503`` otherwise."""

    authentication_classes: list = []  # a bad Authorization header is ignored
    permission_classes = [AllowAny]
    throttle_classes: list = []

    @extend_schema(auth=[], responses={200: HealthSerializer, 503: OpenApiResponse(HealthSerializer)})
    def get(self, request: Request) -> Response:
        ok = database_ok()
        body = {
            "status": "ok" if ok else "error",
            "db": "ok" if ok else "error",
            "time": timezone.localtime(timezone.now()).replace(microsecond=0).isoformat(),
        }
        return Response(body, status=200 if ok else 503)


class SchemaAccess(BasePermission):
    """Public while ``DEBUG`` is on; ADMIN only (JWT or Django admin session) when it is off."""

    def has_permission(self, request, view) -> bool:
        if settings.DEBUG:
            return True
        return role_of(request.user) == Role.ADMIN


class _DocsAccessMixin:
    authentication_classes = [RevocableJWTAuthentication, SessionAuthentication]
    permission_classes = [SchemaAccess]
    throttle_classes: list = []


class SchemaView(_DocsAccessMixin, SpectacularAPIView):
    """OpenAPI 3 document: JSON by default, ``?format=yaml`` for YAML.

    ``OpenApiJsonRenderer2`` answers ``Accept: application/json`` (the Web App / script clients send it);
    without it such a request got ``406`` instead of the schema or a JSON ``401`` / ``403``.
    """

    renderer_classes = [OpenApiJsonRenderer, OpenApiJsonRenderer2, OpenApiYamlRenderer]


class DocsView(_DocsAccessMixin, SpectacularSwaggerView):
    """Swagger UI (assets from ``drf-spectacular-sidecar``, no CDN)."""

    url_name = "api-schema"

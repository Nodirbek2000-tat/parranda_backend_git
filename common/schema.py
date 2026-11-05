"""Shared OpenAPI pieces (drf-spectacular) for request/response documentation."""

from __future__ import annotations

from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import OpenApiParameter, OpenApiResponse, inline_serializer
from rest_framework import serializers

DetailSerializer = inline_serializer("Detail", fields={"detail": serializers.CharField()})

ERROR_400 = OpenApiResponse(description="Validatsiya xatosi: {\"maydon\": [\"xabar\"]} yoki {\"detail\": \"...\"}")
ERROR_401 = OpenApiResponse(DetailSerializer, description="Tizimga kirilmagan / login yoki parol noto‘g‘ri")
ERROR_403 = OpenApiResponse(DetailSerializer, description="Ruxsat yo‘q / hisob bloklangan")
ERROR_404 = OpenApiResponse(DetailSerializer, description="Topilmadi")


def query(name: str, kind=str, description: str = "", **kwargs) -> OpenApiParameter:
    return OpenApiParameter(name, kind, OpenApiParameter.QUERY, required=False, description=description, **kwargs)


DATE_FROM = query("date_from", OpenApiTypes.DATE, "Sanadan (YYYY-MM-DD, shu kun ham kiradi)")
DATE_TO = query("date_to", OpenApiTypes.DATE, "Sanagacha (YYYY-MM-DD, shu kun ham kiradi)")

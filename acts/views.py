"""Dalolatnomalar API (API_CONTRACT_V4 §6): parse input, apply access rules, serialize."""

from __future__ import annotations

from django.http import HttpResponse
from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import OpenApiResponse, extend_schema
from rest_framework import status
from rest_framework.exceptions import NotFound
from rest_framework.generics import GenericAPIView
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView

from common import query
from common.pagination import StandardPagination
from common.permissions import IsAuthenticatedRole
from common.roles import Role
from common.schema import ERROR_400, ERROR_403, ERROR_404, DetailSerializer
from common.schema import query as query_param

from . import pdf, services
from .models import ActKind
from .serializers import SNAPSHOT_SCHEMA, ActDetailSerializer, ActRequestSerializer, ActSerializer

MSG_NOT_FOUND = "Dalolatnoma topilmadi."
ERROR_409 = OpenApiResponse(DetailSerializer, description="Vazifa hali tasdiqlanmagan")
REQUEST_PARAMETERS = [
    query_param("kind", enum=ActKind.values),
    query_param("role", enum=Role.values),
    query_param("user_id", int),
    query_param("household_id", int),
    query_param("task_id", int),
    query_param("date_from", OpenApiTypes.DATE, "Standart: joriy oyning 1-kuni"),
    query_param("date_to", OpenApiTypes.DATE, "Standart: bugun"),
]


def _act(request: Request, pk: int):
    act = services.visible_acts(request.user).filter(pk=pk).first()
    if act is None:
        raise NotFound(MSG_NOT_FOUND)
    return act


class ActPreviewView(APIView):
    """``GET /api/acts/preview``: the snapshot that ``POST /api/acts`` would freeze (nothing is saved)."""

    permission_classes = [IsAuthenticatedRole]

    @extend_schema(parameters=REQUEST_PARAMETERS,
                   responses={200: SNAPSHOT_SCHEMA, 400: ERROR_400, 403: ERROR_403, 404: ERROR_404, 409: ERROR_409})
    def get(self, request: Request) -> Response:
        serializer = ActRequestSerializer(data=request.query_params.dict())
        serializer.is_valid(raise_exception=True)
        act_request = services.resolve(request.user, serializer.validated_data)
        snapshot, _template = services.build_snapshot(act_request)
        return Response(snapshot)


class ActListView(GenericAPIView):
    """``GET /api/acts`` (paged) and ``POST /api/acts`` (201, snapshot frozen)."""

    permission_classes = [IsAuthenticatedRole]
    pagination_class = StandardPagination
    serializer_class = ActSerializer

    def get_queryset(self):
        qs = services.visible_acts(self.request.user)
        kind = query.get_choice(self.request, "kind", ActKind)
        if kind:
            qs = qs.filter(kind=kind)
        role = query.get_choice(self.request, "role", Role)
        if role:
            qs = qs.filter(role=role)
        household = query.get_int(self.request, "household")
        if household is not None:
            qs = qs.filter(household_id=household)
        return qs.order_by("-created_at", "-id")

    @extend_schema(
        parameters=[query_param("kind", enum=ActKind.values), query_param("role", enum=Role.values),
                    query_param("household", int)],
        responses={200: ActSerializer(many=True)},
    )
    def get(self, request: Request) -> Response:
        page = self.paginate_queryset(self.get_queryset())
        return self.get_paginated_response(ActSerializer(page, many=True).data)

    @extend_schema(request=ActRequestSerializer,
                   responses={201: ActDetailSerializer, 400: ERROR_400, 403: ERROR_403, 404: ERROR_404, 409: ERROR_409})
    def post(self, request: Request) -> Response:
        serializer = ActRequestSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        act = services.create_act(services.resolve(request.user, serializer.validated_data))
        return Response(ActDetailSerializer(_act(request, act.pk)).data, status=status.HTTP_201_CREATED)


class ActDetailView(APIView):
    permission_classes = [IsAuthenticatedRole]

    @extend_schema(responses={200: ActDetailSerializer, 404: ERROR_404})
    def get(self, request: Request, pk: int) -> Response:
        return Response(ActDetailSerializer(_act(request, pk)).data)


class ActPdfView(APIView):
    permission_classes = [IsAuthenticatedRole]

    @extend_schema(responses={(200, "application/pdf"): OpenApiTypes.BINARY, 404: ERROR_404})
    def get(self, request: Request, pk: int) -> HttpResponse:
        act = _act(request, pk)
        content = pdf.build(act.snapshot)
        services.record_export(act, request.user)
        response = HttpResponse(content, content_type="application/pdf")
        response["Content-Disposition"] = f'attachment; filename="{act.code}.pdf"'
        return response

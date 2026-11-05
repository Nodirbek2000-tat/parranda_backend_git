from __future__ import annotations

from drf_spectacular.utils import extend_schema
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView

from common.permissions import IsOwnerRole, IsStaffRole
from farm.services import household_of

from . import services
from .schema import AdminDashboardSerializer, OwnerDashboardSerializer


class AdminDashboardView(APIView):
    permission_classes = [IsStaffRole]

    @extend_schema(responses=AdminDashboardSerializer)
    def get(self, request: Request) -> Response:
        return Response(services.admin_dashboard())


class OwnerDashboardView(APIView):
    permission_classes = [IsOwnerRole]

    @extend_schema(responses=OwnerDashboardSerializer)
    def get(self, request: Request) -> Response:
        return Response(services.owner_dashboard(household_of(request.user)))

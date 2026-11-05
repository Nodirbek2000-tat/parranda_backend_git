from __future__ import annotations

from drf_spectacular.utils import OpenApiParameter, OpenApiResponse, extend_schema
from rest_framework.exceptions import NotFound, PermissionDenied
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView

from accounts.models import User
from common.permissions import IsOwnerRole, IsStaffRole
from farm.services import household_of
from notifications.bot_auth import HasBotToken
from accounts.authentication import MSG_MUST_CHANGE_PASSWORD
from notifications.views import MSG_USER_NOT_FOUND

from . import services
from .schema import (
    OVERVIEW_FILTER_PARAMETERS,
    PERIOD_PARAMETERS,
    THRESHOLD_PARAMETER,
    BotSummarySerializer,
    HouseholdStatsSerializer,
    OverviewStatsSerializer,
    WarehouseStatsSerializer,
)


class OverviewStatsView(APIView):
    """``GET /api/stats/overview`` — company statistics (ADMIN, WAREHOUSE)."""

    permission_classes = [IsStaffRole]

    @extend_schema(
        parameters=[*PERIOD_PARAMETERS, THRESHOLD_PARAMETER, *OVERVIEW_FILTER_PARAMETERS],
        responses=OverviewStatsSerializer,
    )
    def get(self, request: Request) -> Response:
        return Response(services.overview(request.query_params))


class WarehouseStatsView(APIView):
    """``GET /api/warehouse/stats`` — stock statistics (ADMIN, WAREHOUSE)."""

    permission_classes = [IsStaffRole]

    @extend_schema(parameters=PERIOD_PARAMETERS, responses=WarehouseStatsSerializer)
    def get(self, request: Request) -> Response:
        return Response(services.warehouse_stats(request.query_params))


class MyStatsView(APIView):
    """``GET /api/my/stats`` — the OWNER's own household statistics."""

    permission_classes = [IsOwnerRole]

    @extend_schema(parameters=PERIOD_PARAMETERS, responses=HouseholdStatsSerializer)
    def get(self, request: Request) -> Response:
        return Response(services.household_stats(household_of(request.user), request.query_params))


class BotSummaryView(APIView):
    """``GET /api/bot/users/{telegram_id}/summary`` (``X-Bot-Token``) — data for the bot ``/reports`` command."""

    authentication_classes: list = []
    permission_classes = [HasBotToken]

    @extend_schema(
        auth=[{"botToken": []}],
        parameters=[OpenApiParameter("telegram_id", int, OpenApiParameter.PATH)],
        responses={200: BotSummarySerializer, 403: OpenApiResponse(description="Token / bloklangan / xonadon yo‘q"),
                   404: OpenApiResponse(description="Telegram bog‘lanmagan")},
    )
    def get(self, request: Request, telegram_id: int) -> Response:
        user = User.objects.select_related("household").filter(telegram_id=telegram_id).first()
        if user is None:
            raise NotFound(MSG_USER_NOT_FOUND)
        if user.is_active and user.must_change_password:
            raise PermissionDenied(MSG_MUST_CHANGE_PASSWORD)
        try:
            return Response(services.bot_summary(user))
        except services.SummaryError as exc:
            raise PermissionDenied(exc.detail) from None

"""Bot-only endpoints. Authenticated exclusively by the ``X-Bot-Token`` header (no JWT)."""

from __future__ import annotations

from drf_spectacular.utils import OpenApiParameter, extend_schema, inline_serializer
from rest_framework import serializers
from rest_framework.exceptions import NotFound, PermissionDenied
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView

from accounts import services as account_services
from accounts.authentication import MSG_MUST_CHANGE_PASSWORD
from accounts.models import User
from accounts.serializers import UserSerializer
from accounts.throttles import UsernameLoginHourThrottle, UsernameLoginMinuteThrottle
from common import query
from common.fields import latitude_field, longitude_field
from common.ids import MAX_DB_INT
from common.roles import Role
from common.schema import ERROR_400, ERROR_403, ERROR_404, DetailSerializer, query as query_param
from farm import services as farm_services
from farm.models import Household

from . import services
from .bot_auth import HasBotToken, TelegramIdLoginThrottle
from .models import Notification
from .serializers import BotInboxSerializer

MSG_USER_NOT_FOUND = "Foydalanuvchi topilmadi"


class TelegramIdSerializer(serializers.Serializer):
    telegram_id = serializers.IntegerField(min_value=1, max_value=MAX_DB_INT)


class LinkSerializer(TelegramIdSerializer):
    username = serializers.CharField(max_length=150)
    password = serializers.CharField(max_length=128, trim_whitespace=False)


class LocationSerializer(TelegramIdSerializer):
    latitude = latitude_field()
    longitude = longitude_field()


class DeliveryResultSerializer(serializers.Serializer):
    ok = serializers.BooleanField()
    # Never reject a delivery report because of its error text: a 400 here would leave the
    # notification PENDING forever. Long texts are truncated by the service.
    error = serializers.CharField(required=False, allow_blank=True, allow_null=True, default="", trim_whitespace=True)

    def validate_error(self, value: str | None) -> str:
        return value or ""


class BotAPIView(APIView):
    authentication_classes: list = []
    permission_classes = [HasBotToken]


BOT_AUTH = [{"botToken": []}]
TELEGRAM_ID_PATH = OpenApiParameter("telegram_id", int, OpenApiParameter.PATH)
BotLocationResponseSerializer = inline_serializer(
    "BotLocationResult",
    fields={
        "household": inline_serializer(
            "BotLocationHousehold",
            fields={
                "id": serializers.IntegerField(),
                "code": serializers.CharField(),
                "name": serializers.CharField(),
                "latitude": serializers.FloatField(),
                "longitude": serializers.FloatField(),
            },
        )
    },
)
PendingNotificationSerializer = inline_serializer(
    "PendingNotification",
    fields={
        "id": serializers.IntegerField(),
        "chat_id": serializers.IntegerField(),
        "text": serializers.CharField(),
        "button_path": serializers.CharField(),
    },
    many=True,
)
DeliveryStatusSerializer = inline_serializer(
    "DeliveryStatus",
    fields={"status": serializers.ChoiceField(choices=[("SENT", "SENT"), ("PENDING", "PENDING"), ("FAILED", "FAILED")])},
)


def _user_by_telegram_id(telegram_id: int) -> User:
    user = User.objects.select_related("household").filter(telegram_id=telegram_id).first()
    if user is None:
        raise NotFound(MSG_USER_NOT_FOUND)
    return user


class BotUserView(BotAPIView):
    @extend_schema(auth=BOT_AUTH, parameters=[TELEGRAM_ID_PATH], responses={200: UserSerializer, 403: ERROR_403,
                                                                             404: ERROR_404})
    def get(self, request: Request, telegram_id: int) -> Response:
        return Response(UserSerializer(_user_by_telegram_id(telegram_id)).data)


class BotLinkView(BotAPIView):
    # Per Telegram id, plus per username (shared with /api/auth/login): many throwaway
    # Telegram accounts cannot multiply the guesses against one account.
    throttle_classes = [TelegramIdLoginThrottle, UsernameLoginMinuteThrottle, UsernameLoginHourThrottle]

    @extend_schema(auth=BOT_AUTH, request=LinkSerializer, responses={200: UserSerializer, 400: ERROR_400,
                                                                     403: ERROR_403})
    def post(self, request: Request) -> Response:
        serializer = LinkSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            user = account_services.link_telegram(**serializer.validated_data)
        except account_services.CredentialsInvalid:
            raise serializers.ValidationError({"detail": account_services.MSG_BAD_CREDENTIALS}) from None
        except account_services.AccountBlocked:
            raise PermissionDenied(account_services.MSG_BLOCKED) from None
        user = User.objects.select_related("household").get(pk=user.pk)
        return Response(UserSerializer(user).data)


class BotUnlinkView(BotAPIView):
    @extend_schema(auth=BOT_AUTH, request=TelegramIdSerializer, responses={200: DetailSerializer, 403: ERROR_403})
    def post(self, request: Request) -> Response:
        serializer = TelegramIdSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        user = User.objects.filter(telegram_id=serializer.validated_data["telegram_id"]).first()
        if user is not None:
            account_services.unlink_telegram(user, actor=user)
        return Response({"detail": "Telegram uzildi"})


class BotLocationView(BotAPIView):
    @extend_schema(
        auth=BOT_AUTH,
        request=LocationSerializer,
        responses={200: BotLocationResponseSerializer, 400: ERROR_400, 403: ERROR_403, 404: ERROR_404},
    )
    def post(self, request: Request) -> Response:
        serializer = LocationSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        user = bot_actor(data["telegram_id"])
        household = Household.objects.filter(owner=user).first() if user.effective_role == Role.OWNER else None
        if household is None:
            raise PermissionDenied("Lokatsiyani faqat xonadon egasi yubora oladi")
        household = farm_services.set_household_location(
            household, data["latitude"], data["longitude"], actor=user
        )
        return Response(
            {
                "household": {
                    "id": household.pk,
                    "code": household.code,
                    "name": household.name,
                    "latitude": float(household.latitude),
                    "longitude": float(household.longitude),
                }
            }
        )


class PendingNotificationsView(BotAPIView):
    @extend_schema(
        auth=BOT_AUTH,
        parameters=[query_param("limit", int, "1…100, standart 20")],
        responses={200: PendingNotificationSerializer, 403: ERROR_403},
    )
    def get(self, request: Request) -> Response:
        limit = query.get_int(request, "limit") or services.PENDING_LIMIT_DEFAULT
        rows = services.pending(limit).values("id", "chat_id", "text", "button_path")
        return Response(list(rows))


class NotificationResultView(BotAPIView):
    @extend_schema(
        auth=BOT_AUTH,
        parameters=[OpenApiParameter("pk", int, OpenApiParameter.PATH)],
        request=DeliveryResultSerializer,
        responses={200: DeliveryStatusSerializer, 403: ERROR_403, 404: ERROR_404},
    )
    def post(self, request: Request, pk: int) -> Response:
        serializer = DeliveryResultSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            notification = services.record_delivery(pk, **serializer.validated_data)
        except Notification.DoesNotExist:
            raise NotFound("Bildirishnoma topilmadi") from None
        return Response({"status": notification.status})


def bot_actor(telegram_id: int) -> User:
    """The active user linked to ``telegram_id`` (V3 §14.1): unknown → 404, blocked → 403.

    A user with a temporary password gets ``403`` too (TZ §3: the password must be changed first;
    the bot shows the Web App button for that).
    """
    user = _user_by_telegram_id(telegram_id)
    if not user.is_active:
        raise PermissionDenied(account_services.MSG_BLOCKED)
    if user.must_change_password:
        raise PermissionDenied(MSG_MUST_CHANGE_PASSWORD)
    return user


class BotInboxView(BotAPIView):
    """Last N inbox rows of the linked user for the bot's ``/notifications`` command (V3 §14.1, §14.3)."""

    @extend_schema(
        auth=BOT_AUTH,
        parameters=[TELEGRAM_ID_PATH, query_param("limit", int, "1…20, standart 10")],
        responses={200: BotInboxSerializer(many=True), 403: ERROR_403, 404: ERROR_404},
    )
    def get(self, request: Request, telegram_id: int) -> Response:
        user = bot_actor(telegram_id)
        limit = max(1, min(query.get_int(request, "limit") or 10, 20))
        rows = services.inbox(user)[:limit]
        return Response(BotInboxSerializer(rows, many=True).data)


ReminderRunSerializer = inline_serializer(
    "ReminderRun",
    fields={"date": serializers.DateField(), "ran": serializers.BooleanField(), "queued": serializers.IntegerField()},
)


class BotReminderRunView(BotAPIView):
    """Reminder tick of the bot worker (every 15 min). The server decides whether to run (V3 §16.1)."""

    @extend_schema(auth=BOT_AUTH, request=None, responses={200: ReminderRunSerializer, 403: ERROR_403})
    def post(self, request: Request) -> Response:
        from . import reminders

        return Response(reminders.run_if_due())

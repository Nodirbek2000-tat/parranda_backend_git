"""JWT endpoints of the notification engine (API_CONTRACT_V3 §9.3, §9.5, §9.6).

- ``/api/notifications*`` — monitor for ADMIN (list, detail, stats, retry, retry-failed).
- ``/api/notification-settings*`` — GET ADMIN, PATCH Super Admin.
- ``/api/my/notifications*`` — the bell of the current user (any role, own rows only).
"""

from __future__ import annotations

from datetime import timedelta

from django.db.models import Count, Q
from django.utils import timezone
from drf_spectacular.utils import OpenApiParameter, extend_schema, inline_serializer
from rest_framework import serializers, status
from rest_framework.exceptions import NotFound, ValidationError
from rest_framework.generics import GenericAPIView
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView

from common import query
from common.exceptions import translate_message
from common.periods import day_range
from common.permissions import IsAdminRole, IsAuthenticatedRole, IsSuperAdmin
from common.schema import DATE_FROM, DATE_TO, ERROR_400, ERROR_403, ERROR_404, query as query_param

from . import registry, services
from .models import Notification, NotificationStatus
from .serializers import (
    MyNotificationSerializer,
    NotificationMonitorDetailSerializer,
    NotificationMonitorSerializer,
    NotificationSettingSerializer,
    NotificationSettingUpdateSerializer,
    RetryFailedSerializer,
)

MSG_NOT_FOUND = "Bildirishnoma topilmadi"
ID_PATH = OpenApiParameter("pk", int, OpenApiParameter.PATH)
STATS_DEFAULT_DAYS = 30


class Conflict(serializers.ValidationError):
    status_code = status.HTTP_409_CONFLICT


def _dates(request: Request):
    date_from = query.get_date(request, "date_from")
    date_to = query.get_date(request, "date_to")
    if date_from and date_to and date_from > date_to:
        raise ValidationError({"date_to": ["Tugash sanasi boshlanish sanasidan oldin bo‘lishi mumkin emas."]})
    return date_from, date_to


# --- Monitor ---------------------------------------------------------------------------------


class NotificationListView(GenericAPIView):
    permission_classes = [IsAdminRole]
    serializer_class = NotificationMonitorSerializer

    def get_queryset(self):
        request = self.request
        qs = Notification.objects.select_related("recipient").order_by("-id")
        status_value = query.get_str(request, "status")
        if status_value:
            if status_value not in NotificationStatus.values:
                raise ValidationError({"status": [f"“{status_value}” ruxsat etilgan qiymat emas."]})
            qs = qs.filter(status=status_value)
        event = query.get_str(request, "event")
        if event:
            qs = qs.filter(event=event)
        recipient = query.get_int(request, "recipient")
        if recipient is not None:
            qs = qs.filter(recipient_id=recipient)
        start, end = day_range(*_dates(request))
        if start is not None:
            qs = qs.filter(created_at__gte=start)
        if end is not None:
            qs = qs.filter(created_at__lt=end)
        search = query.get_str(request, "search")
        if search:
            qs = qs.filter(
                Q(related_op__icontains=search)
                | Q(recipient__first_name__icontains=search)
                | Q(recipient__last_name__icontains=search)
                | Q(recipient__username__icontains=search)
            )
        return qs

    @extend_schema(
        parameters=[
            query_param("status", enum=NotificationStatus.values),
            query_param("event"),
            query_param("recipient", int),
            DATE_FROM,
            DATE_TO,
            query_param("search", description="operatsiya ID yoki qabul qiluvchi ismi"),
        ],
        responses=NotificationMonitorSerializer(many=True),
    )
    def get(self, request: Request) -> Response:
        page = self.paginate_queryset(self.get_queryset())
        return self.get_paginated_response(self.get_serializer(page, many=True).data)


class NotificationDetailView(APIView):
    permission_classes = [IsAdminRole]

    @extend_schema(parameters=[ID_PATH], responses={200: NotificationMonitorDetailSerializer, 404: ERROR_404})
    def get(self, request: Request, pk: int) -> Response:
        row = Notification.objects.select_related("recipient").filter(pk=pk).first()
        if row is None:
            raise NotFound(MSG_NOT_FOUND)
        return Response(NotificationMonitorDetailSerializer(row).data)


NotificationStatsSerializer = inline_serializer(
    "NotificationStats",
    fields={
        "PENDING": serializers.IntegerField(),
        "SENT": serializers.IntegerField(),
        "FAILED": serializers.IntegerField(),
        "SKIPPED": serializers.IntegerField(),
        "by_event": inline_serializer(
            "NotificationStatsByEvent",
            fields={
                "event": serializers.CharField(),
                "display": serializers.CharField(),
                "PENDING": serializers.IntegerField(),
                "SENT": serializers.IntegerField(),
                "FAILED": serializers.IntegerField(),
                "SKIPPED": serializers.IntegerField(),
            },
            many=True,
        ),
    },
)


class NotificationStatsView(APIView):
    permission_classes = [IsAdminRole]

    @extend_schema(parameters=[DATE_FROM, DATE_TO], responses=NotificationStatsSerializer)
    def get(self, request: Request) -> Response:
        date_from, date_to = _dates(request)
        if date_from is None and date_to is None:
            date_to = timezone.localdate()
            date_from = date_to - timedelta(days=STATS_DEFAULT_DAYS - 1)
        start, end = day_range(date_from, date_to)
        qs = Notification.objects.all()
        if start is not None:
            qs = qs.filter(created_at__gte=start)
        if end is not None:
            qs = qs.filter(created_at__lt=end)
        totals = {value: 0 for value in NotificationStatus.values}
        by_event: dict[str, dict] = {}
        for row in qs.values("event", "status").annotate(n=Count("id")).order_by():
            totals[row["status"]] = totals.get(row["status"], 0) + row["n"]
            item = by_event.setdefault(
                row["event"],
                {"event": row["event"], "display": registry.display(row["event"]),
                 **{value: 0 for value in NotificationStatus.values}},
            )
            item[row["status"]] += row["n"]
        order = {key: index for index, key in enumerate(registry.EVENTS)}
        events = sorted(by_event.values(), key=lambda item: (order.get(item["event"], 10_000), item["event"]))
        return Response({**totals, "by_event": events})


class NotificationRetryView(APIView):
    permission_classes = [IsAdminRole]

    @extend_schema(
        parameters=[ID_PATH],
        request=None,
        responses={200: NotificationMonitorSerializer, 400: ERROR_400, 404: ERROR_404, 409: ERROR_400},
    )
    def post(self, request: Request, pk: int) -> Response:
        try:
            row = services.retry(pk, actor=request.user)
        except Notification.DoesNotExist:
            raise NotFound(MSG_NOT_FOUND) from None
        except services.RetryConflict as exc:
            raise Conflict({"detail": str(exc)}) from None
        except services.RetryNotLinked as exc:
            raise ValidationError({"detail": str(exc)}) from None
        return Response(NotificationMonitorSerializer(row).data)


RetryFailedResult = inline_serializer(
    "RetryFailedResult", fields={"retried": serializers.IntegerField(), "skipped": serializers.IntegerField()}
)


class NotificationRetryFailedView(APIView):
    permission_classes = [IsAdminRole]

    @extend_schema(request=RetryFailedSerializer, responses={200: RetryFailedResult, 400: ERROR_400})
    def post(self, request: Request) -> Response:
        serializer = RetryFailedSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        if data["date_from"] and data["date_to"] and data["date_from"] > data["date_to"]:
            raise ValidationError({"date_to": ["Tugash sanasi boshlanish sanasidan oldin bo‘lishi mumkin emas."]})
        return Response(services.retry_failed(actor=request.user, **data))


# --- Settings --------------------------------------------------------------------------------


class NotificationSettingListView(APIView):
    permission_classes = [IsAdminRole]

    @extend_schema(responses=NotificationSettingSerializer(many=True))
    def get(self, request: Request) -> Response:
        rows = [NotificationSettingSerializer.build(d, row) for d, row in services.settings_rows()]
        return Response(NotificationSettingSerializer(rows, many=True).data)


class NotificationSettingDetailView(APIView):
    permission_classes = [IsAdminRole, IsSuperAdmin]

    @extend_schema(
        parameters=[OpenApiParameter("event", str, OpenApiParameter.PATH)],
        request=NotificationSettingUpdateSerializer,
        responses={200: NotificationSettingSerializer, 400: ERROR_400, 403: ERROR_403, 404: ERROR_404},
    )
    def patch(self, request: Request, event: str) -> Response:
        if event not in registry.EVENTS:
            raise NotFound(translate_message("Not found."))
        serializer = NotificationSettingUpdateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        row = services.update_setting(event, dict(serializer.validated_data), actor=request.user)
        row = type(row).objects.select_related("updated_by").get(pk=row.pk)
        return Response(NotificationSettingSerializer(NotificationSettingSerializer.build(registry.get(event), row)).data)


# --- My notifications (bell) -----------------------------------------------------------------


class MyNotificationListView(GenericAPIView):
    permission_classes = [IsAuthenticatedRole]
    serializer_class = MyNotificationSerializer

    def get_queryset(self):
        qs = services.inbox(self.request.user)
        if query.get_bool(self.request, "unread"):
            qs = qs.filter(read_at__isnull=True)
        return qs

    @extend_schema(parameters=[query_param("unread", bool)], responses=MyNotificationSerializer(many=True))
    def get(self, request: Request) -> Response:
        page = self.paginate_queryset(self.get_queryset())
        return self.get_paginated_response(self.get_serializer(page, many=True).data)


UnreadCountSerializer = inline_serializer("UnreadCount", fields={"count": serializers.IntegerField()})


class MyUnreadCountView(APIView):
    permission_classes = [IsAuthenticatedRole]

    @extend_schema(responses=UnreadCountSerializer)
    def get(self, request: Request) -> Response:
        return Response({"count": services.unread_count(request.user)})


class MyNotificationReadView(APIView):
    permission_classes = [IsAuthenticatedRole]

    @extend_schema(parameters=[ID_PATH], request=None, responses={200: MyNotificationSerializer, 404: ERROR_404})
    def post(self, request: Request, pk: int) -> Response:
        try:
            row = services.mark_read(request.user, pk)
        except Notification.DoesNotExist:
            raise NotFound(MSG_NOT_FOUND) from None
        return Response(MyNotificationSerializer(row).data)


ReadAllSerializer = inline_serializer("ReadAllResult", fields={"updated": serializers.IntegerField()})


class MyNotificationReadAllView(APIView):
    permission_classes = [IsAuthenticatedRole]

    @extend_schema(request=None, responses=ReadAllSerializer)
    def post(self, request: Request) -> Response:
        return Response({"updated": services.mark_all_read(request.user)})



# --- Personal preferences (TZ §11.3) ----------------------------------------------------------------

class NotificationPreferenceSerializer(serializers.Serializer):
    event = serializers.CharField()
    display = serializers.CharField()
    description = serializers.CharField()
    enabled = serializers.BooleanField()
    telegram_enabled = serializers.BooleanField()
    global_enabled = serializers.BooleanField(help_text="Super Admin sozlamasi: hodisa umuman yoqilganmi")
    global_telegram = serializers.BooleanField(help_text="Super Admin sozlamasi: Telegram yoqilganmi")


class PreferenceUpdateSerializer(serializers.Serializer):
    enabled = serializers.BooleanField(required=False)
    telegram_enabled = serializers.BooleanField(required=False)


def _preference_rows(user) -> list[dict]:
    from common.roles import role_of

    from .models import NotificationPreference

    own = {row.event: row for row in NotificationPreference.objects.filter(user=user)}
    result = []
    for definition in services.events_for_role(role_of(user) or ""):
        config = services.effective_settings(definition.key)
        row = own.get(definition.key)
        result.append(
            {
                "event": definition.key,
                "display": definition.display,
                "description": definition.description,
                "enabled": row.enabled if row is not None else True,
                "telegram_enabled": row.telegram_enabled if row is not None else True,
                "global_enabled": config.enabled,
                "global_telegram": config.telegram_enabled,
            }
        )
    return result


class MyNotificationPreferencesView(APIView):
    """``GET /api/my/notification-preferences`` — the events this user can receive, with the own switches."""

    permission_classes = [IsAuthenticatedRole]

    @extend_schema(responses=NotificationPreferenceSerializer(many=True))
    def get(self, request: Request) -> Response:
        return Response(_preference_rows(request.user))


class MyNotificationPreferenceView(APIView):
    """``PATCH /api/my/notification-preferences/{event}`` ``{enabled?, telegram_enabled?}``."""

    permission_classes = [IsAuthenticatedRole]

    @extend_schema(
        request=PreferenceUpdateSerializer,
        responses={200: NotificationPreferenceSerializer, 400: ERROR_400, 404: ERROR_404},
    )
    def patch(self, request: Request, event: str) -> Response:
        from common.roles import role_of

        allowed = {definition.key for definition in services.events_for_role(role_of(request.user) or "")}
        if event not in allowed:
            raise NotFound("Hodisa topilmadi.")
        serializer = PreferenceUpdateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        services.update_preference(request.user, event, serializer.validated_data)
        return Response(next(row for row in _preference_rows(request.user) if row["event"] == event))

"""API views of the logistics app (API_CONTRACT_V3 §7, §14.1): parse/validate input, call ``services``, serialize."""

from __future__ import annotations

from drf_spectacular.utils import extend_schema, inline_serializer
from rest_framework import serializers as drf_serializers
from rest_framework import status
from rest_framework.exceptions import NotFound, PermissionDenied
from rest_framework.generics import GenericAPIView
from rest_framework.parsers import MultiPartParser
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.throttling import SimpleRateThrottle
from rest_framework.views import APIView

from common import query
from common.ids import MAX_DB_INT
from common.pagination import StandardPagination
from common.permissions import IsRoleIn, ReadOnlyFor
from common.roles import Role, role_of
from common.schema import DATE_FROM, DATE_TO, ERROR_400, ERROR_403, ERROR_404, DetailSerializer
from common.schema import query as query_param
from core import files
from core.serializers import AttachmentSerializer
from notifications.bot_auth import HasBotToken
from notifications.views import BOT_AUTH, TELEGRAM_ID_PATH, bot_actor

from . import selectors, services
from . import serializers as s
from .models import StatusSource

ERROR_409 = DetailSerializer

TaskReadRoles = IsRoleIn(Role.ADMIN, Role.WAREHOUSE, Role.LOGISTICS, Role.OWNER)
FleetPermission = IsRoleIn(Role.ADMIN, Role.LOGISTICS) | ReadOnlyFor(Role.WAREHOUSE)


def _detail(task_id: int, request: Request) -> Response:
    task = selectors.task_queryset(request.user).filter(pk=task_id).first()
    if task is None:
        raise NotFound(services.MSG_NOT_FOUND)
    context = {"request": request, "files": files.files_by_object("LogisticsTask", [task.pk])}
    return Response(s.LogisticsTaskDetailSerializer(task, context=context).data)


# --- Vehicles / drivers ------------------------------------------------------------------------


class VehicleListView(GenericAPIView):
    permission_classes = [FleetPermission]
    pagination_class = StandardPagination
    serializer_class = s.VehicleSerializer

    def get_queryset(self):
        qs = selectors.vehicles_queryset()
        term = query.get_str(self.request, "search")
        if term:
            qs = qs.filter(plate_number__icontains=term) | qs.filter(model__icontains=term)
        is_active = query.get_bool(self.request, "is_active")
        if is_active is not None:
            qs = qs.filter(is_active=is_active)
        return qs.order_by("plate_number", "id")

    @extend_schema(parameters=[query_param("search"), query_param("is_active", bool)],
                   responses={200: s.VehicleSerializer(many=True), 403: ERROR_403})
    def get(self, request: Request) -> Response:
        page = self.paginate_queryset(self.get_queryset())
        return self.get_paginated_response(s.VehicleSerializer(page, many=True).data)

    @extend_schema(request=s.VehicleWriteSerializer, responses={201: s.VehicleSerializer, 400: ERROR_400})
    def post(self, request: Request) -> Response:
        serializer = s.VehicleWriteSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        vehicle = services.create_vehicle(actor=request.user, data=serializer.validated_data)
        vehicle = selectors.vehicles_queryset().get(pk=vehicle.pk)
        return Response(s.VehicleSerializer(vehicle).data, status=status.HTTP_201_CREATED)


class VehicleDetailView(APIView):
    permission_classes = [FleetPermission]

    def _get(self, pk: int):
        vehicle = selectors.vehicles_queryset().filter(pk=pk).first()
        if vehicle is None:
            raise NotFound("Transport topilmadi.")
        return vehicle

    @extend_schema(responses={200: s.VehicleSerializer, 404: ERROR_404})
    def get(self, request: Request, pk: int) -> Response:
        return Response(s.VehicleSerializer(self._get(pk)).data)

    @extend_schema(request=s.VehicleWriteSerializer, responses={200: s.VehicleSerializer, 400: ERROR_400})
    def patch(self, request: Request, pk: int) -> Response:
        self._get(pk)
        serializer = s.VehicleWriteSerializer(data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        services.update_vehicle(actor=request.user, vehicle_id=pk, changes=dict(serializer.validated_data))
        return Response(s.VehicleSerializer(self._get(pk)).data)

    @extend_schema(request=None, responses={204: None, 403: ERROR_403, 404: ERROR_404, 409: ERROR_409})
    def delete(self, request: Request, pk: int) -> Response:
        """Delete a vehicle (kept in task history, hidden everywhere; see services.delete_vehicle)."""
        self._get(pk)
        services.delete_vehicle(actor=request.user, vehicle_id=pk)
        return Response(status=status.HTTP_204_NO_CONTENT)


class DriverListView(GenericAPIView):
    permission_classes = [FleetPermission]
    pagination_class = StandardPagination
    serializer_class = s.DriverSerializer

    def get_queryset(self):
        qs = selectors.drivers_queryset()
        term = query.get_str(self.request, "search")
        if term:
            qs = qs.filter(full_name__icontains=term) | qs.filter(phone__icontains=term) | qs.filter(
                license_no__icontains=term
            )
        is_active = query.get_bool(self.request, "is_active")
        if is_active is not None:
            qs = qs.filter(is_active=is_active)
        return qs.order_by("full_name", "id")

    @extend_schema(parameters=[query_param("search"), query_param("is_active", bool)],
                   responses={200: s.DriverSerializer(many=True), 403: ERROR_403})
    def get(self, request: Request) -> Response:
        page = self.paginate_queryset(self.get_queryset())
        return self.get_paginated_response(s.DriverSerializer(page, many=True).data)

    @extend_schema(request=s.DriverWriteSerializer, responses={201: s.DriverSerializer, 400: ERROR_400})
    def post(self, request: Request) -> Response:
        serializer = s.DriverWriteSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        driver = services.create_driver(actor=request.user, data=serializer.validated_data)
        driver = selectors.drivers_queryset().get(pk=driver.pk)
        return Response(s.DriverSerializer(driver).data, status=status.HTTP_201_CREATED)


class DriverDetailView(APIView):
    permission_classes = [FleetPermission]

    def _get(self, pk: int):
        driver = selectors.drivers_queryset().filter(pk=pk).first()
        if driver is None:
            raise NotFound("Haydovchi topilmadi.")
        return driver

    @extend_schema(responses={200: s.DriverSerializer, 404: ERROR_404})
    def get(self, request: Request, pk: int) -> Response:
        return Response(s.DriverSerializer(self._get(pk)).data)

    @extend_schema(request=s.DriverWriteSerializer, responses={200: s.DriverSerializer, 400: ERROR_400})
    def patch(self, request: Request, pk: int) -> Response:
        self._get(pk)
        serializer = s.DriverWriteSerializer(data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        services.update_driver(actor=request.user, driver_id=pk, changes=dict(serializer.validated_data))
        return Response(s.DriverSerializer(self._get(pk)).data)

    @extend_schema(request=None, responses={204: None, 403: ERROR_403, 404: ERROR_404, 409: ERROR_409})
    def delete(self, request: Request, pk: int) -> Response:
        """Delete a driver (kept in task history, hidden everywhere; see services.delete_driver)."""
        self._get(pk)
        services.delete_driver(actor=request.user, driver_id=pk)
        return Response(status=status.HTTP_204_NO_CONTENT)


# --- Tasks -----------------------------------------------------------------------------------------

TASK_FILTERS = [
    query_param("status", str, "Vergul bilan: NEW,ASSIGNED,ACCEPTED,IN_TRANSIT,DELIVERED,CONFIRMED,CANCELLED"),
    query_param("household", int),
    query_param("driver", int),
    query_param("vehicle", int),
    query_param("responsible", str, "“me” yoki foydalanuvchi ID"),
    query_param("mine", bool),
    query_param("overdue", bool),
    DATE_FROM,
    DATE_TO,
    query_param("search"),
]


class TaskListView(GenericAPIView):
    pagination_class = StandardPagination
    serializer_class = s.LogisticsTaskSerializer

    def get_permissions(self):
        if self.request.method == "POST":
            return [IsRoleIn(Role.ADMIN)()]
        return [TaskReadRoles()]

    @extend_schema(parameters=TASK_FILTERS, responses={200: s.LogisticsTaskSerializer(many=True), 403: ERROR_403})
    def get(self, request: Request) -> Response:
        qs = selectors.ordered(selectors.filter_tasks(selectors.task_queryset(request.user), request))
        page = self.paginate_queryset(qs)
        context = {"request": request, "files": files.files_by_object("LogisticsTask", [task.pk for task in page])}
        return self.get_paginated_response(s.LogisticsTaskSerializer(page, many=True, context=context).data)

    @extend_schema(request=s.TaskCreateSerializer, responses={201: s.LogisticsTaskDetailSerializer, 400: ERROR_400})
    def post(self, request: Request) -> Response:
        serializer = s.TaskCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        task = services.create_task(
            actor=request.user,
            household=data["household"],
            items=[services.ItemInput(product=row["product"], quantity=row["quantity"]) for row in data["items"]],
            planned_date=data["planned_date"],
            note=data["note"],
            vehicle=data["vehicle"].pk if data["vehicle"] else None,
            driver=data["driver"].pk if data["driver"] else None,
            responsible=data["responsible"].pk if data["responsible"] else None,
        )
        response = _detail(task.pk, request)
        response.status_code = status.HTTP_201_CREATED
        return response


class TaskDetailView(APIView):
    def get_permissions(self):
        if self.request.method == "PATCH":
            return [IsRoleIn(Role.ADMIN)()]
        return [TaskReadRoles()]

    @extend_schema(responses={200: s.LogisticsTaskDetailSerializer, 404: ERROR_404})
    def get(self, request: Request, pk: int) -> Response:
        return _detail(pk, request)

    @extend_schema(request=s.TaskUpdateSerializer, responses={200: s.LogisticsTaskDetailSerializer, 409: ERROR_409})
    def patch(self, request: Request, pk: int) -> Response:
        serializer = s.TaskUpdateSerializer(data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        services.update_task(actor=request.user, task_id=pk, changes=dict(serializer.validated_data))
        return _detail(pk, request)


class _TaskActionView(APIView):
    """``POST /api/logistics/tasks/{id}/<action>``; the role/worker checks live in the services."""

    roles: tuple[str, ...] = ()
    serializer_class: type[drf_serializers.Serializer] = s.NoteSerializer

    def get_permissions(self):
        return [IsRoleIn(*self.roles)()]

    def run(self, request: Request, pk: int, data: dict):  # pragma: no cover - overridden
        raise NotImplementedError

    def post(self, request: Request, pk: int) -> Response:
        serializer = self.serializer_class(data=request.data)
        serializer.is_valid(raise_exception=True)
        self.run(request, pk, serializer.validated_data)
        return _detail(pk, request)


def _action_schema(serializer):
    return extend_schema(
        request=serializer,
        responses={200: s.LogisticsTaskDetailSerializer, 400: ERROR_400, 403: ERROR_403, 404: ERROR_404,
                   409: ERROR_409},
    )


class TaskAssignView(_TaskActionView):
    roles = (Role.ADMIN, Role.LOGISTICS)
    serializer_class = s.AssignSerializer

    @_action_schema(s.AssignSerializer)
    def post(self, request, pk):
        return super().post(request, pk)

    def run(self, request, pk, data):
        services.assign(
            actor=request.user,
            task_id=pk,
            vehicle=data["vehicle"].pk,
            driver=data["driver"].pk,
            responsible=data["responsible"].pk if data["responsible"] else None,
        )


class TaskAcceptView(_TaskActionView):
    roles = (Role.ADMIN, Role.LOGISTICS)
    serializer_class = s.NoteSerializer

    @_action_schema(s.NoteSerializer)
    def post(self, request, pk):
        return super().post(request, pk)

    def run(self, request, pk, data):
        services.accept(actor=request.user, task_id=pk, note=data["note"])


class TaskStartView(_TaskActionView):
    roles = (Role.ADMIN, Role.LOGISTICS)
    serializer_class = s.StartSerializer

    @_action_schema(s.StartSerializer)
    def post(self, request, pk):
        return super().post(request, pk)

    def run(self, request, pk, data):
        services.start(actor=request.user, task_id=pk, latitude=data["latitude"], longitude=data["longitude"],
                       note=data["note"])


class TaskDeliverView(_TaskActionView):
    roles = (Role.ADMIN, Role.LOGISTICS)
    serializer_class = s.DeliverSerializer

    @_action_schema(s.DeliverSerializer)
    def post(self, request, pk):
        return super().post(request, pk)

    def run(self, request, pk, data):
        services.deliver(
            actor=request.user,
            task_id=pk,
            file_ids=data["file_ids"],
            recipient_name=data["recipient_name"],
            note=data["note"],
            latitude=data["latitude"],
            longitude=data["longitude"],
        )


class TaskConfirmView(_TaskActionView):
    roles = (Role.ADMIN, Role.OWNER)
    serializer_class = s.ConfirmSerializer

    @_action_schema(s.ConfirmSerializer)
    def post(self, request, pk):
        return super().post(request, pk)

    def run(self, request, pk, data):
        services.confirm(actor=request.user, task_id=pk, received_ok=data["received_ok"],
                         issue_note=data["issue_note"])


class TaskCancelView(_TaskActionView):
    roles = (Role.ADMIN,)
    serializer_class = s.CancelSerializer

    @_action_schema(s.CancelSerializer)
    def post(self, request, pk):
        return super().post(request, pk)

    def run(self, request, pk, data):
        services.cancel_task(actor=request.user, task_id=pk, reason=data["reason"])


class SummaryView(APIView):
    permission_classes = [IsRoleIn(Role.ADMIN, Role.WAREHOUSE, Role.LOGISTICS)]

    @extend_schema(responses={200: s.SummarySerializer, 403: ERROR_403})
    def get(self, request: Request) -> Response:
        return Response(selectors.summary(request.user))


# --- Bot endpoints (§14.1, §14.2) -----------------------------------------------------------------


class BotActionThrottle(SimpleRateThrottle):
    """``bot_action`` rate (60/min) per Telegram id (body or path)."""

    scope = "bot_action"

    def get_cache_key(self, request, view) -> str | None:
        data = request.data if isinstance(request.data, dict) else {}
        ident = str(data.get("telegram_id") or view.kwargs.get("telegram_id") or "").strip()
        if not ident:
            return None
        return self.cache_format % {"scope": self.scope, "ident": ident}


class BotUploadThrottle(SimpleRateThrottle):
    """``uploads`` (60/hour) per **linked user** for ``POST /api/bot/files``.

    The bot request is anonymous, so ``ScopedRateThrottle`` would key it by the bot's IP and every driver would
    share one bucket. The key here is the linked user's id, i.e. the same bucket as that user's JWT uploads
    (``throttle_uploads_<user pk>``). Unknown ``telegram_id`` → no throttle (the view answers 400/404).
    """

    scope = "uploads"

    def get_cache_key(self, request, view) -> str | None:
        from accounts.models import User

        raw = str(request.data.get("telegram_id") or "").strip()
        if not raw.isdigit() or not 0 < int(raw) <= MAX_DB_INT:
            return None
        user_id = User.objects.filter(telegram_id=int(raw)).values_list("pk", flat=True).first()
        if user_id is None:
            return None
        return self.cache_format % {"scope": self.scope, "ident": user_id}


class BotTelegramSerializer(drf_serializers.Serializer):
    telegram_id = drf_serializers.IntegerField(min_value=1, max_value=MAX_DB_INT)


class BotStartSerializer(BotTelegramSerializer, s.StartSerializer):
    pass


class BotDeliverSerializer(BotTelegramSerializer, s.DeliverSerializer):
    pass


class BotConfirmSerializer(BotTelegramSerializer, s.ConfirmSerializer):
    pass


class _BotView(APIView):
    authentication_classes: list = []
    permission_classes = [HasBotToken]
    throttle_classes = [BotActionThrottle]


def _short(task_id: int, user) -> dict:
    task = selectors.task_queryset(user).filter(pk=task_id).first()
    if task is None:
        raise NotFound(services.MSG_NOT_FOUND)
    return s.LogisticsTaskShortSerializer(task, context={"user": user}).data


def _check_bot_role(user, *roles: str) -> None:
    if role_of(user) not in roles:
        raise PermissionDenied("Sizda bu amal uchun ruxsat yo‘q.")


class _BotTaskActionView(_BotView):
    serializer_class: type[drf_serializers.Serializer] = BotTelegramSerializer
    roles: tuple[str, ...] = (Role.ADMIN, Role.LOGISTICS)

    def run(self, user, pk: int, data: dict):  # pragma: no cover - overridden
        raise NotImplementedError

    def post(self, request: Request, pk: int) -> Response:
        serializer = self.serializer_class(data=request.data)
        serializer.is_valid(raise_exception=True)
        user = bot_actor(serializer.validated_data["telegram_id"])
        _check_bot_role(user, *self.roles)
        self.run(user, pk, serializer.validated_data)
        return Response(_short(pk, user))


def _bot_schema(serializer):
    return extend_schema(
        auth=BOT_AUTH,
        request=serializer,
        responses={200: s.LogisticsTaskShortSerializer, 400: ERROR_400, 403: ERROR_403, 404: ERROR_404,
                   409: ERROR_409},
    )


class BotTaskAcceptView(_BotTaskActionView):
    @_bot_schema(BotTelegramSerializer)
    def post(self, request, pk):
        return super().post(request, pk)

    def run(self, user, pk, data):
        services.accept(actor=user, task_id=pk, source=StatusSource.BOT)


class BotTaskStartView(_BotTaskActionView):
    serializer_class = BotStartSerializer

    @_bot_schema(BotStartSerializer)
    def post(self, request, pk):
        return super().post(request, pk)

    def run(self, user, pk, data):
        services.start(actor=user, task_id=pk, latitude=data["latitude"], longitude=data["longitude"],
                       note=data["note"], source=StatusSource.BOT)


class BotTaskDeliverView(_BotTaskActionView):
    serializer_class = BotDeliverSerializer

    @_bot_schema(BotDeliverSerializer)
    def post(self, request, pk):
        return super().post(request, pk)

    def run(self, user, pk, data):
        services.deliver(actor=user, task_id=pk, file_ids=data["file_ids"], recipient_name=data["recipient_name"],
                         note=data["note"], latitude=data["latitude"], longitude=data["longitude"],
                         source=StatusSource.BOT)


class BotTaskConfirmView(_BotTaskActionView):
    serializer_class = BotConfirmSerializer
    roles = (Role.ADMIN, Role.OWNER)

    @_bot_schema(BotConfirmSerializer)
    def post(self, request, pk):
        return super().post(request, pk)

    def run(self, user, pk, data):
        services.confirm(actor=user, task_id=pk, received_ok=data["received_ok"], issue_note=data["issue_note"],
                         source=StatusSource.BOT)


class BotFileUploadView(files.UploadSizeLimitMixin, _BotView):
    """``POST /api/bot/files`` (multipart: ``telegram_id``, ``purpose``, ``file``) → ``201 File``."""

    parser_classes = [MultiPartParser]
    throttle_classes = [BotUploadThrottle]

    @extend_schema(
        auth=BOT_AUTH,
        request={"multipart/form-data": inline_serializer(
            "BotFileUpload",
            fields={"telegram_id": drf_serializers.IntegerField(), "purpose": drf_serializers.CharField(),
                    "file": drf_serializers.FileField()},
        )},
        responses={201: AttachmentSerializer, 400: ERROR_400, 403: ERROR_403, 404: ERROR_404},
    )
    def post(self, request: Request) -> Response:
        serializer = BotTelegramSerializer(data={"telegram_id": request.data.get("telegram_id")})
        serializer.is_valid(raise_exception=True)
        user = bot_actor(serializer.validated_data["telegram_id"])
        attachment = files.upload(actor=user, file=request.FILES.get("file"), purpose=request.data.get("purpose"))
        return Response(AttachmentSerializer(attachment).data, status=status.HTTP_201_CREATED)


class BotTasksView(_BotView):
    """``GET /api/bot/users/{tid}/tasks`` — role-specific ``/tasks`` payload (§14.2)."""

    throttle_classes: list = []

    @extend_schema(auth=BOT_AUTH, parameters=[TELEGRAM_ID_PATH],
                   responses={200: inline_serializer("BotTasks", fields={"role": drf_serializers.CharField()}),
                              403: ERROR_403, 404: ERROR_404})
    def get(self, request: Request, telegram_id: int) -> Response:
        from . import bot

        user = bot_actor(telegram_id)
        return Response(bot.tasks_payload(user))


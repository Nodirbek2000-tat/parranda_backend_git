"""API views of the Konstruktor (API_CONTRACT_V4 §1–§3): parse/validate input, call ``services``, serialize.

Access: ``/api/builder/*`` writes — Super Admin only; ``/api/builder/meta`` and blueprint reads — any ADMIN
(a non-super ADMIN sees active blueprints only). Runtime (``/api/forms*``, ``/api/tasks*``,
``/api/process-runs*``) — every role, with object rules in ``services``.
"""

from __future__ import annotations

from django.db.models import Prefetch, Q, QuerySet
from drf_spectacular.utils import OpenApiParameter, OpenApiResponse, extend_schema
from rest_framework import status
from rest_framework.exceptions import NotFound, PermissionDenied, ValidationError
from rest_framework.generics import GenericAPIView
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView

from accounts.models import User
from common import query
from common.permissions import MSG_NO_PERMISSION, IsAdminRole, IsAuthenticatedRole, IsSuperAdmin
from common.roles import Role, is_super_admin, role_of
from common.schema import ERROR_400, ERROR_403, ERROR_404, query as query_param
from core import search as core_search
from farm.models import Household

from . import schemas, services
from .models import Blueprint, BlueprintKind, BlueprintStatus, ProcessRunStatus, TaskStatus, UserAccess
from .serializers import (
    BlueprintCreateSerializer,
    BlueprintListItemSerializer,
    BlueprintSerializer,
    BlueprintUpdateSerializer,
    CommentSerializer,
    ConfigVersionSerializer,
    FormListItemSerializer,
    MetaSerializer,
    TaskNoteSerializer,
    ProcessRunSerializer,
    ProcessStartSerializer,
    ProcessCancelSerializer,
    TaskReturnSerializer,
    RevertSerializer,
    SubmissionCreateSerializer,
    SubmissionSerializer,
    TaskCountsSerializer,
    BuilderTaskCreateSerializer,
    TaskSerializer,
    TaskSubmitSerializer,
    UserAccessRowSerializer,
    UserAccessUpdateSerializer,
    runs_payload,
    submissions_payload,
    tasks_payload,
)

ID_PATH = OpenApiParameter("pk", int, OpenApiParameter.PATH)
ERROR_409 = OpenApiResponse(description="Holat yoki bog‘liqlik amalga ruxsat bermaydi: {\"detail\": \"...\"}")
ERRORS = {400: ERROR_400, 403: ERROR_403, 404: ERROR_404, 409: ERROR_409}

SuperAdminOnly = [IsAdminRole, IsSuperAdmin]


def _pairs(items) -> list[dict[str, str]]:
    return [{"value": value, "label": label} for value, label in items]


# --- Meta (§1.3) ---------------------------------------------------------------------------------


class MetaView(APIView):
    permission_classes = [IsAdminRole]

    @extend_schema(responses=MetaSerializer)
    def get(self, request: Request) -> Response:
        profiles = Blueprint.objects.filter(kind=BlueprintKind.ROLE).exclude(
            status=BlueprintStatus.ARCHIVED
        ).order_by("name", "id")
        role_refs = _pairs(Role.choices) + [
            {"value": f"profile:{p.pk}", "label": f"{p.name} (profil)"} for p in profiles
        ]
        districts = sorted(
            {d.strip() for d in Household.objects.values_list("district", flat=True).distinct() if d and d.strip()}
        )
        return Response({
            "kinds": _pairs(BlueprintKind.choices),
            "statuses": _pairs(BlueprintStatus.choices),
            "field_types": _pairs(schemas.FIELD_TYPES),
            "modules": _pairs(schemas.MODULES),
            "actions": _pairs(schemas.ACTIONS),
            "system_roles": _pairs(Role.choices),
            "role_refs": role_refs,
            "placeholders": _pairs(schemas.PLACEHOLDERS),
            "districts": districts,
        })


# --- Blueprints (§1.1) ---------------------------------------------------------------------------


def _blueprints(user) -> QuerySet[Blueprint]:
    qs = Blueprint.objects.select_related("created_by", "pending_by")
    if not is_super_admin(user):
        qs = qs.filter(status=BlueprintStatus.ACTIVE)
    return qs


def _get_blueprint(request: Request, pk: int) -> Blueprint:
    bp = _blueprints(request.user).filter(pk=pk).first()
    if bp is None:
        raise NotFound("Topilmadi.")
    return bp


def blueprint_response(request: Request, pk: int, code: int = status.HTTP_200_OK) -> Response:
    return Response(BlueprintSerializer(_get_blueprint(request, pk)).data, status=code)


class _ReadAdminWriteSuper:
    def get_permissions(self):
        if self.request.method in ("GET", "HEAD", "OPTIONS"):
            return [IsAdminRole()]
        return [permission() for permission in SuperAdminOnly]


class BlueprintListView(_ReadAdminWriteSuper, GenericAPIView):
    serializer_class = BlueprintListItemSerializer

    def get_queryset(self):
        request = self.request
        qs = _blueprints(request.user)
        kind = query.get_choice(request, "kind", BlueprintKind)
        if kind:
            qs = qs.filter(kind=kind)
        status_value = query.get_choice(request, "status", BlueprintStatus)
        if status_value:
            qs = qs.filter(status=status_value)
        search = query.get_str(request, "search")
        if search:
            qs = qs.filter(Q(name__icontains=search) | Q(description__icontains=search))
        return qs.order_by("kind", "name", "id")

    @extend_schema(
        parameters=[
            query_param("kind", enum=BlueprintKind.values),
            query_param("status", enum=BlueprintStatus.values),
            query_param("search"),
        ],
        responses={200: BlueprintListItemSerializer(many=True), 403: ERROR_403},
    )
    def get(self, request: Request) -> Response:
        page = self.paginate_queryset(self.get_queryset())
        context = {"usage": services.usage_map(page)}
        return self.get_paginated_response(BlueprintListItemSerializer(page, many=True, context=context).data)

    @extend_schema(request=BlueprintCreateSerializer, responses={201: BlueprintSerializer, **ERRORS})
    def post(self, request: Request) -> Response:
        serializer = BlueprintCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        bp = services.create_blueprint(actor=request.user, **serializer.validated_data)
        return blueprint_response(request, bp.pk, status.HTTP_201_CREATED)


class BlueprintDetailView(_ReadAdminWriteSuper, APIView):
    @extend_schema(parameters=[ID_PATH], responses={200: BlueprintSerializer, 404: ERROR_404})
    def get(self, request: Request, pk: int) -> Response:
        return blueprint_response(request, pk)

    @extend_schema(parameters=[ID_PATH], request=BlueprintUpdateSerializer,
                   responses={200: BlueprintSerializer, **ERRORS})
    def patch(self, request: Request, pk: int) -> Response:
        serializer = BlueprintUpdateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        services.update_blueprint(actor=request.user, pk=pk, changes=dict(serializer.validated_data))
        return blueprint_response(request, pk)

    @extend_schema(parameters=[ID_PATH], responses={204: None, **ERRORS})
    def delete(self, request: Request, pk: int) -> Response:
        services.delete_blueprint(actor=request.user, pk=pk)
        return Response(status=status.HTTP_204_NO_CONTENT)


def _action_view(name: str, handler, *, request_serializer=None, created: bool = False):
    """``POST /api/builder/blueprints/{id}/<action>`` (Super Admin)."""

    class _View(APIView):
        permission_classes = SuperAdminOnly

        @extend_schema(
            operation_id=f"builder_blueprints_{name.replace('-', '_')}",
            parameters=[ID_PATH],
            request=request_serializer,
            responses={201 if created else 200: BlueprintSerializer, **ERRORS},
        )
        def post(self, request: Request, pk: int) -> Response:
            payload = {}
            if request_serializer is not None:
                serializer = request_serializer(data=request.data)
                serializer.is_valid(raise_exception=True)
                payload = dict(serializer.validated_data)
            bp = handler(actor=request.user, pk=pk, **payload)
            return blueprint_response(request, bp.pk, status.HTTP_201_CREATED if created else status.HTTP_200_OK)

    _View.__name__ = f"Blueprint{name.title().replace('-', '')}View"
    _View.__qualname__ = _View.__name__
    return _View


BlueprintTestView = _action_view("test", services.test_blueprint)
BlueprintActivateView = _action_view("activate", services.activate_blueprint, request_serializer=CommentSerializer)
BlueprintDiscardView = _action_view("discard-pending", services.discard_pending)
BlueprintArchiveView = _action_view("archive", services.archive_blueprint)
BlueprintRestoreView = _action_view("restore", services.restore_blueprint)
BlueprintRevertView = _action_view("revert", services.revert_blueprint, request_serializer=RevertSerializer)
BlueprintDuplicateView = _action_view("duplicate", services.duplicate_blueprint, created=True)


class BlueprintVersionsView(APIView):
    permission_classes = SuperAdminOnly

    @extend_schema(parameters=[ID_PATH], responses={200: ConfigVersionSerializer(many=True), 404: ERROR_404})
    def get(self, request: Request, pk: int) -> Response:
        bp = _get_blueprint(request, pk)
        return Response(ConfigVersionSerializer(services.blueprint_versions(bp), many=True).data)


# --- User access (§2) ----------------------------------------------------------------------------


def _access_users() -> QuerySet[User]:
    return (
        User.objects.filter(deleted_at__isnull=True)
        .exclude(role=Role.OWNER, is_superuser=False)
        .select_related("access", "access__profile")
        .prefetch_related(Prefetch("access__households", queryset=Household.objects.order_by("id")))
    )


class UserAccessListView(GenericAPIView):
    permission_classes = SuperAdminOnly
    serializer_class = UserAccessRowSerializer

    @extend_schema(
        parameters=[query_param("search"), query_param("profile", int)],
        responses={200: UserAccessRowSerializer(many=True), 403: ERROR_403},
    )
    def get(self, request: Request) -> Response:
        qs = _access_users().order_by("id")
        search = query.get_str(request, "search")
        if search:
            qs = qs.filter(
                Q(username__icontains=search) | Q(first_name__icontains=search) | Q(last_name__icontains=search)
            )
        profile = query.get_int(request, "profile")
        if profile is not None:
            qs = qs.filter(access__profile_id=profile)
        page = self.paginate_queryset(qs)
        return self.get_paginated_response(UserAccessRowSerializer(page, many=True).data)


class UserAccessDetailView(APIView):
    permission_classes = SuperAdminOnly

    @extend_schema(
        parameters=[OpenApiParameter("user_id", int, OpenApiParameter.PATH)],
        request=UserAccessUpdateSerializer,
        responses={200: UserAccessRowSerializer, **ERRORS},
    )
    def put(self, request: Request, user_id: int) -> Response:
        serializer = UserAccessUpdateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        services.set_user_access(actor=request.user, user_id=user_id, **serializer.validated_data)
        return Response(UserAccessRowSerializer(_access_users().get(pk=user_id)).data)


# --- Forms (§3.1) --------------------------------------------------------------------------------


class FormListView(APIView):
    permission_classes = [IsAuthenticatedRole]

    @extend_schema(responses=FormListItemSerializer(many=True))
    def get(self, request: Request) -> Response:
        forms = services.fillable_forms(request.user)
        return Response(FormListItemSerializer(forms, many=True, context={"request": request}).data)


class FormSubmitView(APIView):
    permission_classes = [IsAuthenticatedRole]

    @extend_schema(parameters=[ID_PATH], request=SubmissionCreateSerializer,
                   responses={201: SubmissionSerializer, **ERRORS})
    def post(self, request: Request, pk: int) -> Response:
        serializer = SubmissionCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        sub = services.submit_form(actor=request.user, form_id=pk, **serializer.validated_data)
        row = services.submissions_visible(request.user).get(pk=sub.pk)
        return Response(submissions_payload([row], request.user)[0], status=status.HTTP_201_CREATED)


class SubmissionListView(GenericAPIView):
    permission_classes = [IsAuthenticatedRole]
    serializer_class = SubmissionSerializer

    @extend_schema(parameters=[query_param("form", int), query_param("household", int)],
                   responses=SubmissionSerializer(many=True))
    def get(self, request: Request) -> Response:
        qs = services.submissions_visible(request.user)
        form = query.get_int(request, "form")
        if form is not None:
            qs = qs.filter(form_id=form)
        household = query.get_int(request, "household")
        if household is not None:
            qs = qs.filter(household_id=household)
        page = self.paginate_queryset(qs.order_by("-created_at", "-id"))
        return self.get_paginated_response(submissions_payload(page, request.user))


class SubmissionDetailView(APIView):
    permission_classes = [IsAuthenticatedRole]

    @extend_schema(parameters=[ID_PATH], responses={200: SubmissionSerializer, 404: ERROR_404})
    def get(self, request: Request, pk: int) -> Response:
        row = services.submissions_visible(request.user).filter(pk=pk).first()
        if row is None:
            raise NotFound("Topilmadi.")
        return Response(submissions_payload([row], request.user)[0])


# --- Tasks (§3.2) --------------------------------------------------------------------------------


def task_response(request: Request, pk: int, code: int = status.HTTP_200_OK) -> Response:
    task = services.tasks_visible(request.user).filter(pk=pk).first()
    if task is None:
        raise NotFound("Vazifa topilmadi.")
    return Response(tasks_payload([task], request.user)[0], status=code)


TASK_VIEWS = ("mine", "approve", "all")


class TaskListView(GenericAPIView):
    permission_classes = [IsAuthenticatedRole]
    serializer_class = TaskSerializer

    def get_queryset(self):
        request, user = self.request, self.request.user
        qs = services.tasks_visible(user)
        view = query.get_str(request, "view") or "mine"
        if view not in TASK_VIEWS:
            raise ValidationError({"view": [f"“{view}” ruxsat etilgan qiymat emas."]})
        if view == "mine":
            qs = qs.filter(services.mine_q(user))
        elif view == "approve":
            qs = qs.filter(services.approve_q(user))
        elif role_of(user) != Role.ADMIN:
            raise PermissionDenied(MSG_NO_PERMISSION)
        status_value = query.get_choice(request, "status", TaskStatus)
        if status_value:
            qs = qs.filter(status=status_value)
        household = query.get_int(request, "household")
        if household is not None:
            qs = qs.filter(household_id=household)
        search = query.get_str(request, "search")
        if search:
            condition = Q(title__icontains=search) | Q(household__name__icontains=search)
            for prefix, field in (("VZ", "pk"), ("XN", "household_id")):
                pk = core_search.code_pk(search, prefix)
                if pk is not None:
                    condition |= Q(**{field: pk})
            qs = qs.filter(condition)
        return qs.order_by("-created_at", "-id")

    @extend_schema(
        parameters=[
            query_param("view", enum=list(TASK_VIEWS)),
            query_param("status", enum=TaskStatus.values),
            query_param("household", int),
            query_param("search"),
        ],
        responses={200: TaskSerializer(many=True), 403: ERROR_403},
    )
    def get(self, request: Request) -> Response:
        page = self.paginate_queryset(self.get_queryset())
        return self.get_paginated_response(tasks_payload(page, request.user))

    @extend_schema(request=BuilderTaskCreateSerializer, responses={201: TaskSerializer, **ERRORS})
    def post(self, request: Request) -> Response:
        if role_of(request.user) != Role.ADMIN:
            raise PermissionDenied(MSG_NO_PERMISSION)
        serializer = BuilderTaskCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        task = services.create_task(actor=request.user, **serializer.validated_data)
        return task_response(request, task.pk, status.HTTP_201_CREATED)


class TaskCountsView(APIView):
    permission_classes = [IsAuthenticatedRole]

    @extend_schema(responses=TaskCountsSerializer)
    def get(self, request: Request) -> Response:
        return Response(services.task_counts(request.user))


class TaskDetailView(APIView):
    permission_classes = [IsAuthenticatedRole]

    @extend_schema(parameters=[ID_PATH], responses={200: TaskSerializer, 404: ERROR_404})
    def get(self, request: Request, pk: int) -> Response:
        return task_response(request, pk)


def _task_action(name: str, handler, request_serializer):
    class _View(APIView):
        permission_classes = [IsAuthenticatedRole]

        @extend_schema(operation_id=f"tasks_{name}", parameters=[ID_PATH], request=request_serializer,
                       responses={200: TaskSerializer, **ERRORS})
        def post(self, request: Request, pk: int) -> Response:
            serializer = request_serializer(data=request.data)
            serializer.is_valid(raise_exception=True)
            handler(actor=request.user, pk=pk, **serializer.validated_data)
            return task_response(request, pk)

    _View.__name__ = f"Task{name.title()}View"
    _View.__qualname__ = _View.__name__
    return _View


TaskSubmitView = _task_action("submit", services.submit_task, TaskSubmitSerializer)
TaskApproveView = _task_action("approve", services.approve_task, TaskNoteSerializer)
TaskReturnView = _task_action("return", services.return_task, TaskReturnSerializer)
TaskCancelView = _task_action("cancel", services.cancel_task, TaskNoteSerializer)


# --- Process runs (§3.3) -------------------------------------------------------------------------


def run_response(request: Request, pk: int, code: int = status.HTTP_200_OK) -> Response:
    run = services.runs_visible(request.user).filter(pk=pk).first()
    if run is None:
        raise NotFound("Jarayon topilmadi.")
    return Response(runs_payload([run])[0], status=code)


class ProcessRunListView(GenericAPIView):
    permission_classes = [IsAuthenticatedRole]
    serializer_class = ProcessRunSerializer

    @extend_schema(
        parameters=[
            query_param("status", enum=ProcessRunStatus.values),
            query_param("household", int),
            query_param("process", int),
        ],
        responses=ProcessRunSerializer(many=True),
    )
    def get(self, request: Request) -> Response:
        qs = services.runs_visible(request.user)
        status_value = query.get_choice(request, "status", ProcessRunStatus)
        if status_value:
            qs = qs.filter(status=status_value)
        household = query.get_int(request, "household")
        if household is not None:
            qs = qs.filter(household_id=household)
        process = query.get_int(request, "process")
        if process is not None:
            qs = qs.filter(process_id=process)
        page = self.paginate_queryset(qs.order_by("-started_at", "-id"))
        return self.get_paginated_response(runs_payload(page))

    @extend_schema(request=ProcessStartSerializer, responses={201: ProcessRunSerializer, **ERRORS})
    def post(self, request: Request) -> Response:
        if role_of(request.user) != Role.ADMIN:
            raise PermissionDenied(MSG_NO_PERMISSION)
        serializer = ProcessStartSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        run = services.start_process(actor=request.user, **serializer.validated_data)
        return run_response(request, run.pk, status.HTTP_201_CREATED)


class ProcessRunDetailView(APIView):
    permission_classes = [IsAuthenticatedRole]

    @extend_schema(parameters=[ID_PATH], responses={200: ProcessRunSerializer, 404: ERROR_404})
    def get(self, request: Request, pk: int) -> Response:
        return run_response(request, pk)


class ProcessRunCancelView(APIView):
    permission_classes = [IsAdminRole]

    @extend_schema(parameters=[ID_PATH], request=ProcessCancelSerializer, responses={200: ProcessRunSerializer, **ERRORS})
    def post(self, request: Request, pk: int) -> Response:
        serializer = ProcessCancelSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        services.cancel_process(actor=request.user, pk=pk, **serializer.validated_data)
        return run_response(request, pk)

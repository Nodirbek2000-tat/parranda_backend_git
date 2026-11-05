"""API views of the vet app (API_CONTRACT_V3 §8): parse/validate input, call ``services``, serialize.

Access (§1.2): ADMIN and VET everything (VET-only actions: complete, treatment create/update, take);
OWNER reads his own household (others → 404) and creates/cancels requests; WAREHOUSE only
``GET /api/vet/vaccinations?batch=``; LOGISTICS → 403.
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import Any

from django.db.models import QuerySet
from drf_spectacular.utils import extend_schema, extend_schema_view, inline_serializer
from rest_framework import serializers, status, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import ValidationError
from rest_framework.permissions import BasePermission
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView

from common import query
from common.ids import ID_REGEX
from common.permissions import MSG_NO_PERMISSION, IsRoleIn
from common.roles import Role, role_of
from common.schema import DATE_FROM, DATE_TO, ERROR_400, ERROR_403, ERROR_404
from common.schema import query as query_param
from core import files
from farm import services as farm_services

from . import selectors, services
from .models import DiseaseStatus, RequestStatus, VetStatus
from .serializers import (
    DiseaseCreateSerializer,
    DiseaseRecordSerializer,
    DiseaseUpdateSerializer,
    RequestCloseSerializer,
    RequestTakeSerializer,
    RequestVisitPlanSerializer,
    TreatmentCreateSerializer,
    TreatmentSerializer,
    TreatmentUpdateSerializer,
    VaccinationCompleteSerializer,
    VaccinationPlanSerializer,
    VaccinationSerializer,
    VaccinationUpdateSerializer,
    VetReasonSerializer,
    VetRequestCreateSerializer,
    VetRequestSerializer,
    VetVisitSerializer,
    VisitCompleteSerializer,
    VisitPlanSerializer,
    VisitUpdateSerializer,
)

A, W, V, O = Role.ADMIN, Role.WAREHOUSE, Role.VET, Role.OWNER

ReadAVO = IsRoleIn(A, V, O)
WriteAV = IsRoleIn(A, V)
OnlyV = IsRoleIn(V)
OnlyO = IsRoleIn(O)
OnlyA = IsRoleIn(A)
StaffOrOwner = IsRoleIn(A, V, O)

MSG_CALENDAR_RANGE = "Davr 62 kundan oshmasligi kerak."
MSG_CALENDAR_ORDER = "Boshlanish sanasi tugash sanasidan keyin bo‘lishi mumkin emas."

ERRORS = {400: ERROR_400, 403: ERROR_403, 404: ERROR_404}


class VaccinationAccess(BasePermission):
    """A, V, O; WAREHOUSE only ``GET`` lists filtered by ``batch`` (batch “Emlash tarixi”, §8.2)."""

    message = MSG_NO_PERMISSION

    def has_permission(self, request, view) -> bool:
        role = role_of(request.user)
        if role in (A, V, O):
            return True
        return (
            role == W
            and request.method in ("GET", "HEAD", "OPTIONS")
            and getattr(view, "action", None) == "list"
            and bool(query.get_str(request, "batch"))
        )


class _VetViewSet(viewsets.GenericViewSet):
    """Shared plumbing: scoped queryset, file map in the serializer context, fresh re-read after writes."""

    lookup_value_regex = ID_REGEX
    object_type = ""
    serializer_class: type[serializers.Serializer]

    def base_queryset(self) -> QuerySet:  # pragma: no cover - overridden
        raise NotImplementedError

    def get_queryset(self) -> QuerySet:
        qs = selectors.scope(self.base_queryset(), self.request.user)
        if self.action == "list":
            qs = self.list_filters(qs)
        return qs

    def list_filters(self, qs: QuerySet) -> QuerySet:
        return qs

    def detail_context(self) -> dict[str, Any]:
        return {}

    def serialize(self, rows, *, many: bool = False, **extra) -> Any:
        items = list(rows) if many else [rows]
        context = {
            "request": self.request,
            "files": files.files_by_object(self.object_type, [row.pk for row in items]),
            **extra,
        }
        return self.serializer_class(items if many else rows, many=many, context=context).data

    def list(self, request: Request) -> Response:
        qs = self.get_queryset()
        page = self.paginate_queryset(qs)
        if page is not None:
            return self.get_paginated_response(self.serialize(page, many=True))
        return Response(self.serialize(qs, many=True))  # pragma: no cover - pagination is global

    def retrieve(self, request: Request, pk: str | None = None) -> Response:
        return Response(self.serialize(self.get_object(), **self.detail_context()))

    def fresh(self, pk: int, *, status_code: int = status.HTTP_200_OK) -> Response:
        obj = self.base_queryset().get(pk=pk)
        return Response(self.serialize(obj, **self.detail_context()), status=status_code)


class _DeleteMixin:
    """``DELETE`` on the detail route → 204 (soft delete, see ``services.delete_record``)."""

    delete_kind = ""

    @extend_schema(request=None, responses={204: None, 403: ERROR_403, 404: ERROR_404})
    def destroy(self, request: Request, pk: str | None = None) -> Response:
        services.delete_record(actor=request.user, kind=self.delete_kind, pk=self.get_object().pk)
        return Response(status=status.HTTP_204_NO_CONTENT)


def _planned_filters(request: Request, qs: QuerySet) -> QuerySet:
    status_value = query.get_choice(request, "status", VetStatus)
    if status_value:
        qs = qs.filter(status=status_value)
    household = query.get_int(request, "household")
    if household is not None:
        qs = qs.filter(household_id=household)
    batch = query.get_int(request, "batch")
    if batch is not None:
        qs = qs.filter(batch_id=batch)
    qs = selectors.vet_filter(qs, request.user, query.get_str(request, "vet"))
    qs = query.apply_date_range(qs, request, "planned_date")
    if query.get_bool(request, "overdue"):
        qs = qs.filter(status=VetStatus.PLANNED, planned_date__lt=selectors.today())
    return qs


PLANNED_FILTERS = [
    query_param("status", enum=VetStatus.values),
    query_param("household", int),
    query_param("batch", int),
    query_param("vet", description="me | <id> | none"),
    DATE_FROM,
    DATE_TO,
    query_param("overdue", bool),
]


# --- Visits (§8.1) --------------------------------------------------------------------------------


@extend_schema_view(
    list=extend_schema(parameters=PLANNED_FILTERS),
    create=extend_schema(request=VisitPlanSerializer, responses={201: VetVisitSerializer, **ERRORS}),
    partial_update=extend_schema(request=VisitUpdateSerializer, responses={200: VetVisitSerializer, **ERRORS}),
    complete=extend_schema(request=VisitCompleteSerializer, responses={200: VetVisitSerializer, **ERRORS}),
    cancel=extend_schema(request=VetReasonSerializer, responses={200: VetVisitSerializer, **ERRORS}),
)
class VetVisitViewSet(_DeleteMixin, _VetViewSet):
    delete_kind = "visit"
    serializer_class = VetVisitSerializer
    object_type = "VetVisit"

    def get_permissions(self):
        if self.action in ("list", "retrieve"):
            return [ReadAVO()]
        if self.action == "complete":
            return [OnlyV()]
        return [WriteAV()]

    def base_queryset(self) -> QuerySet:
        return selectors.visits()

    def list_filters(self, qs: QuerySet) -> QuerySet:
        return _planned_filters(self.request, qs).order_by("planned_date", "id")

    def create(self, request: Request) -> Response:
        serializer = VisitPlanSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        visit = services.plan_visit(actor=request.user, data=dict(serializer.validated_data))
        return self.fresh(visit.pk, status_code=status.HTTP_201_CREATED)

    def partial_update(self, request: Request, pk: str | None = None) -> Response:
        visit = self.get_object()
        serializer = VisitUpdateSerializer(data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        services.update_visit(actor=request.user, pk=visit.pk, changes=dict(serializer.validated_data))
        return self.fresh(visit.pk)

    @action(detail=True, methods=["post"])
    def complete(self, request: Request, pk: str | None = None) -> Response:
        visit = self.get_object()
        serializer = VisitCompleteSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        services.complete_visit(actor=request.user, pk=visit.pk, data=dict(serializer.validated_data))
        return self.fresh(visit.pk)

    @action(detail=True, methods=["post"])
    def cancel(self, request: Request, pk: str | None = None) -> Response:
        visit = self.get_object()
        serializer = VetReasonSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        services.cancel_visit(actor=request.user, pk=visit.pk, reason=serializer.validated_data["reason"])
        return self.fresh(visit.pk)


# --- Vaccinations (§8.2) --------------------------------------------------------------------------

VaccinationCampaignSerializer = inline_serializer(
    "VaccinationCampaign", fields={"created": VaccinationSerializer(many=True)}
)


@extend_schema_view(
    list=extend_schema(
        parameters=[*PLANNED_FILTERS, query_param("due_within", int, "PLANNED, planned_date ≤ bugun + N")]
    ),
    create=extend_schema(
        request=VaccinationPlanSerializer,
        responses={201: VaccinationSerializer, 400: ERROR_400, 403: ERROR_403},
        description="Bitta xonadon → `Vaccination`; `household_ids` (ommaviy emlash) → `{\"created\": [...]}`.",
    ),
    partial_update=extend_schema(
        request=VaccinationUpdateSerializer, responses={200: VaccinationSerializer, **ERRORS}
    ),
    complete=extend_schema(request=VaccinationCompleteSerializer, responses={200: VaccinationSerializer, **ERRORS}),
    cancel=extend_schema(request=VetReasonSerializer, responses={200: VaccinationSerializer, **ERRORS}),
)
class VaccinationViewSet(_DeleteMixin, _VetViewSet):
    delete_kind = "vaccination"
    serializer_class = VaccinationSerializer
    object_type = "Vaccination"

    def get_permissions(self):
        if self.action in ("list", "retrieve"):
            return [VaccinationAccess()]
        if self.action == "complete":
            return [OnlyV()]
        return [WriteAV()]

    def base_queryset(self) -> QuerySet:
        return selectors.vaccinations()

    def list_filters(self, qs: QuerySet) -> QuerySet:
        qs = _planned_filters(self.request, qs)
        due_within = query.get_int(self.request, "due_within")
        if due_within is not None:
            if not 0 <= due_within <= 365:
                raise ValidationError({"due_within": ["0 dan 365 gacha butun son kiriting."]})
            qs = qs.filter(
                status=VetStatus.PLANNED, planned_date__lte=selectors.today() + timedelta(days=due_within)
            )
        return qs.order_by("planned_date", "id")

    def create(self, request: Request) -> Response:
        serializer = VaccinationPlanSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = dict(serializer.validated_data)
        created = services.plan_vaccinations(actor=request.user, data=data)
        if data.get("household_ids"):
            rows = list(self.base_queryset().filter(pk__in=[row.pk for row in created]).order_by("id"))
            return Response({"created": self.serialize(rows, many=True)}, status=status.HTTP_201_CREATED)
        return self.fresh(created[0].pk, status_code=status.HTTP_201_CREATED)

    def partial_update(self, request: Request, pk: str | None = None) -> Response:
        vaccination = self.get_object()
        serializer = VaccinationUpdateSerializer(data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        services.update_vaccination(actor=request.user, pk=vaccination.pk, changes=dict(serializer.validated_data))
        return self.fresh(vaccination.pk)

    @action(detail=True, methods=["post"])
    def complete(self, request: Request, pk: str | None = None) -> Response:
        vaccination = self.get_object()
        serializer = VaccinationCompleteSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        services.complete_vaccination(actor=request.user, pk=vaccination.pk, data=dict(serializer.validated_data))
        return self.fresh(vaccination.pk)

    @action(detail=True, methods=["post"])
    def cancel(self, request: Request, pk: str | None = None) -> Response:
        vaccination = self.get_object()
        serializer = VetReasonSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        services.cancel_vaccination(
            actor=request.user, pk=vaccination.pk, reason=serializer.validated_data["reason"]
        )
        return self.fresh(vaccination.pk)


# --- Diseases (§8.3) ------------------------------------------------------------------------------


@extend_schema_view(
    list=extend_schema(
        parameters=[
            query_param("status", enum=DiseaseStatus.values),
            query_param("household", int),
            query_param("batch", int),
            query_param("active", bool, "true → OPEN va TREATMENT"),
            query_param("visit", int, "tekshiruv bo‘yicha qayd etilganlar"),
            DATE_FROM,
            DATE_TO,
        ]
    ),
    create=extend_schema(request=DiseaseCreateSerializer, responses={201: DiseaseRecordSerializer, **ERRORS}),
    partial_update=extend_schema(
        request=DiseaseUpdateSerializer, responses={200: DiseaseRecordSerializer, 409: ERROR_400, **ERRORS}
    ),
)
class DiseaseViewSet(_DeleteMixin, _VetViewSet):
    delete_kind = "disease"
    serializer_class = DiseaseRecordSerializer
    object_type = "DiseaseRecord"

    def get_permissions(self):
        if self.action in ("list", "retrieve"):
            return [ReadAVO()]
        return [WriteAV()]

    def base_queryset(self) -> QuerySet:
        return selectors.diseases()

    def detail_context(self) -> dict[str, Any]:
        return {"with_treatments": True}

    def list_filters(self, qs: QuerySet) -> QuerySet:
        request = self.request
        status_value = query.get_choice(request, "status", DiseaseStatus)
        if status_value:
            qs = qs.filter(status=status_value)
        if query.get_bool(request, "active"):
            qs = qs.exclude(status=DiseaseStatus.RESOLVED)
        household = query.get_int(request, "household")
        if household is not None:
            qs = qs.filter(household_id=household)
        batch = query.get_int(request, "batch")
        if batch is not None:
            qs = qs.filter(batch_id=batch)
        visit = query.get_int(request, "visit")
        if visit is not None:
            qs = qs.filter(visit_id=visit)
        qs = query.apply_date_range(qs, request, "diagnosed_date")
        return qs.order_by("-diagnosed_date", "-id")

    def create(self, request: Request) -> Response:
        serializer = DiseaseCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        disease = services.register_disease(actor=request.user, data=dict(serializer.validated_data))
        return self.fresh(disease.pk, status_code=status.HTTP_201_CREATED)

    def partial_update(self, request: Request, pk: str | None = None) -> Response:
        disease = self.get_object()
        serializer = DiseaseUpdateSerializer(data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        services.update_disease(actor=request.user, pk=disease.pk, changes=dict(serializer.validated_data))
        return self.fresh(disease.pk)


# --- Treatments (§8.4) ----------------------------------------------------------------------------


@extend_schema_view(
    list=extend_schema(
        parameters=[
            query_param("household", int),
            query_param("disease", int),
            query_param("batch", int),
            query_param("active", bool, "true → tugash sanasi yo‘q yoki bugundan keyin"),
            DATE_FROM,
            DATE_TO,
        ]
    ),
    create=extend_schema(request=TreatmentCreateSerializer, responses={201: TreatmentSerializer, **ERRORS}),
    partial_update=extend_schema(request=TreatmentUpdateSerializer, responses={200: TreatmentSerializer, **ERRORS}),
)
class TreatmentViewSet(_VetViewSet):
    serializer_class = TreatmentSerializer
    object_type = "Treatment"

    def get_permissions(self):
        if self.action in ("list", "retrieve"):
            return [ReadAVO()]
        return [OnlyV()]

    def base_queryset(self) -> QuerySet:
        return selectors.treatments()

    def list_filters(self, qs: QuerySet) -> QuerySet:
        request = self.request
        for name in ("household", "disease", "batch"):
            value = query.get_int(request, name)
            if value is not None:
                qs = qs.filter(**{f"{name}_id": value})
        if query.get_bool(request, "active"):
            from django.db.models import Q

            qs = qs.filter(Q(end_date__isnull=True) | Q(end_date__gte=selectors.today()))
        qs = query.apply_date_range(qs, request, "start_date")
        return qs.order_by("-start_date", "-id")

    def create(self, request: Request) -> Response:
        serializer = TreatmentCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        treatment = services.prescribe_treatment(actor=request.user, data=dict(serializer.validated_data))
        return self.fresh(treatment.pk, status_code=status.HTTP_201_CREATED)

    def partial_update(self, request: Request, pk: str | None = None) -> Response:
        treatment = self.get_object()
        serializer = TreatmentUpdateSerializer(data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        services.update_treatment(actor=request.user, pk=treatment.pk, changes=dict(serializer.validated_data))
        return self.fresh(treatment.pk)


# --- Owner requests (§8.5) ------------------------------------------------------------------------


@extend_schema_view(
    list=extend_schema(
        parameters=[
            query_param("status", enum=RequestStatus.values),
            query_param("household", int),
            query_param("vet", description="me | <id> | none"),
            query_param("open", bool, "true → NEW va IN_PROGRESS"),
        ]
    ),
    create=extend_schema(request=VetRequestCreateSerializer, responses={201: VetRequestSerializer, **ERRORS}),
    take=extend_schema(request=RequestTakeSerializer, responses={200: VetRequestSerializer, **ERRORS}),
    close=extend_schema(request=RequestCloseSerializer, responses={200: VetRequestSerializer, **ERRORS}),
    plan_visit=extend_schema(request=RequestVisitPlanSerializer, responses={201: VetVisitSerializer, **ERRORS}),
)
class VetRequestViewSet(_DeleteMixin, _VetViewSet):
    delete_kind = "request"
    serializer_class = VetRequestSerializer
    object_type = "VetRequest"

    def get_permissions(self):
        if self.action in ("list", "retrieve", "close"):
            return [StaffOrOwner()]
        if self.action == "create":
            return [OnlyO()]
        if self.action == "take":
            return [OnlyV()]
        if self.action == "destroy":
            return [OnlyA()]
        return [WriteAV()]

    def base_queryset(self) -> QuerySet:
        return selectors.requests()

    def list_filters(self, qs: QuerySet) -> QuerySet:
        request = self.request
        status_value = query.get_choice(request, "status", RequestStatus)
        if status_value:
            qs = qs.filter(status=status_value)
        if query.get_bool(request, "open"):
            qs = qs.exclude(status=RequestStatus.CLOSED)
        household = query.get_int(request, "household")
        if household is not None:
            qs = qs.filter(household_id=household)
        qs = selectors.vet_filter(qs, request.user, query.get_str(request, "vet"))
        return qs.order_by("-created_at", "-id")

    def create(self, request: Request) -> Response:
        serializer = VetRequestCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        row = services.create_request(actor=request.user, text=data["text"], file_ids=data["file_ids"])
        return self.fresh(row.pk, status_code=status.HTTP_201_CREATED)

    @action(detail=True, methods=["post"])
    def take(self, request: Request, pk: str | None = None) -> Response:
        row = self.get_object()
        serializer = RequestTakeSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        services.take_request(actor=request.user, pk=row.pk, response=serializer.validated_data.get("response", ""))
        return self.fresh(row.pk)

    @action(detail=True, methods=["post"])
    def close(self, request: Request, pk: str | None = None) -> Response:
        row = self.get_object()
        serializer = RequestCloseSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        services.close_request(actor=request.user, pk=row.pk, response=serializer.validated_data.get("response"))
        return self.fresh(row.pk)

    @action(detail=True, methods=["post"], url_path="plan-visit")
    def plan_visit(self, request: Request, pk: str | None = None) -> Response:
        row = self.get_object()
        serializer = RequestVisitPlanSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = {**serializer.validated_data, "request": row, "household": None}
        visit = services.plan_visit(actor=request.user, data=data)
        obj = selectors.visits().get(pk=visit.pk)
        context = {"request": request, "files": {}}
        return Response(VetVisitSerializer(obj, context=context).data, status=status.HTTP_201_CREATED)


# --- Calendar, summary, my vet (§8.7, §8.8) -------------------------------------------------------


def _calendar_range(request: Request) -> tuple[date, date]:
    date_from = query.get_date(request, "date_from")
    date_to = query.get_date(request, "date_to")
    today = selectors.today()
    if date_from is None and date_to is None:
        date_from = today.replace(day=1)
        next_month = (date_from + timedelta(days=32)).replace(day=1)
        date_to = next_month - timedelta(days=1)
    elif date_from is None:
        date_from = date_to.replace(day=1)
    elif date_to is None:
        date_to = min(date_from + timedelta(days=30), date_from + timedelta(days=selectors.CALENDAR_MAX_DAYS - 1))
    if date_from > date_to:
        raise ValidationError({"non_field_errors": [MSG_CALENDAR_ORDER]})
    if (date_to - date_from).days + 1 > selectors.CALENDAR_MAX_DAYS:
        raise ValidationError({"non_field_errors": [MSG_CALENDAR_RANGE]})
    return date_from, date_to


_HouseholdShort = inline_serializer(
    "VetHouseholdShort",
    fields={"id": serializers.IntegerField(), "code": serializers.CharField(), "name": serializers.CharField()},
)
_UserRef = inline_serializer(
    "VetUserRef", fields={"id": serializers.IntegerField(), "full_name": serializers.CharField()}, allow_null=True
)
CalendarDaySerializer = inline_serializer(
    "VetCalendarDay",
    fields={
        "date": serializers.DateField(),
        "visits": inline_serializer(
            "VetCalendarVisit",
            fields={
                "id": serializers.IntegerField(),
                "code": serializers.CharField(),
                "household": _HouseholdShort,
                "status": serializers.CharField(),
                "vet": _UserRef,
                "is_overdue": serializers.BooleanField(),
            },
            many=True,
        ),
        "vaccinations": inline_serializer(
            "VetCalendarVaccination",
            fields={
                "id": serializers.IntegerField(),
                "code": serializers.CharField(),
                "household": _HouseholdShort,
                "vaccine_name": serializers.CharField(),
                "status": serializers.CharField(),
                "is_overdue": serializers.BooleanField(),
            },
            many=True,
        ),
    },
    many=True,
)


class VetCalendarView(APIView):
    """``GET /api/vet/calendar`` (A, V): one item per day, zero-filled, ≤ 62 days, default = current month."""

    permission_classes = [WriteAV]

    @extend_schema(
        parameters=[DATE_FROM, DATE_TO, query_param("vet", description="me | <id> | none")],
        responses={200: CalendarDaySerializer, 400: ERROR_400, 403: ERROR_403},
    )
    def get(self, request: Request) -> Response:
        date_from, date_to = _calendar_range(request)
        return Response(
            selectors.calendar(date_from, date_to, user=request.user, vet=query.get_str(request, "vet"))
        )


_Counts = serializers.DictField(child=serializers.IntegerField())
VetSummarySerializer = inline_serializer(
    "VetSummary",
    fields={
        "date": serializers.DateField(),
        "visits": _Counts,
        "vaccinations": _Counts,
        "diseases": _Counts,
        "requests": _Counts,
        "today_visits": serializers.ListField(child=serializers.DictField()),
        "overdue_visits": serializers.ListField(child=serializers.DictField()),
        "due_vaccinations": serializers.ListField(child=serializers.DictField()),
        "overdue_vaccinations": serializers.ListField(child=serializers.DictField()),
        "new_requests": serializers.ListField(child=serializers.DictField()),
    },
)


class VetSummaryView(APIView):
    permission_classes = [WriteAV]

    @extend_schema(responses={200: VetSummarySerializer, 403: ERROR_403})
    def get(self, request: Request) -> Response:
        return Response(selectors.summary(request.user))


MyVetSerializer = inline_serializer(
    "MyVet",
    fields={
        "household": _HouseholdShort,
        "upcoming_visits": serializers.ListField(child=serializers.DictField()),
        "upcoming_vaccinations": serializers.ListField(child=serializers.DictField()),
        "recent_conclusions": VetVisitSerializer(many=True),
        "open_diseases": DiseaseRecordSerializer(many=True),
        "active_treatments": TreatmentSerializer(many=True),
        "requests": VetRequestSerializer(many=True),
    },
)


class MyVetView(APIView):
    """``GET /api/my/vet`` (OWNER): everything vet-related of the own household on one screen."""

    permission_classes = [OnlyO]

    @extend_schema(responses={200: MyVetSerializer, 403: ERROR_403})
    def get(self, request: Request) -> Response:
        from django.db.models import Q

        household = farm_services.household_of(request.user)
        today = selectors.today()
        limit = selectors.OWNER_UPCOMING_LIMIT
        visits_planned = list(
            selectors.visits().filter(household=household, status=VetStatus.PLANNED).order_by("planned_date", "id")[
                :limit
            ]
        )
        vaccinations_planned = list(
            selectors.vaccinations()
            .filter(household=household, status=VetStatus.PLANNED)
            .order_by("planned_date", "id")[:limit]
        )
        conclusions = list(
            selectors.visits().filter(household=household, status=VetStatus.DONE).order_by("-done_at", "-id")[:limit]
        )
        open_diseases = list(
            selectors.diseases()
            .filter(household=household)
            .exclude(status=DiseaseStatus.RESOLVED)
            .order_by("-diagnosed_date", "-id")[:10]
        )
        treatments = list(
            selectors.treatments()
            .filter(household=household)
            .filter(Q(end_date__isnull=True) | Q(end_date__gte=today))
            .order_by("-start_date", "-id")[:10]
        )
        requests = list(
            selectors.requests().filter(household=household).order_by("-created_at", "-id")[
                : selectors.OWNER_REQUESTS_LIMIT
            ]
        )

        def ser(cls, rows, object_type):
            context = {
                "request": request,
                "files": files.files_by_object(object_type, [row.pk for row in rows]),
            }
            return cls(rows, many=True, context=context).data

        return Response(
            {
                "household": selectors.household_short(household),
                "upcoming_visits": [selectors.visit_short(v) for v in visits_planned],
                "upcoming_vaccinations": [selectors.vaccination_short(v) for v in vaccinations_planned],
                "recent_conclusions": ser(VetVisitSerializer, conclusions, "VetVisit"),
                "open_diseases": ser(DiseaseRecordSerializer, open_diseases, "DiseaseRecord"),
                "active_treatments": ser(TreatmentSerializer, treatments, "Treatment"),
                "requests": ser(VetRequestSerializer, requests, "VetRequest"),
            }
        )

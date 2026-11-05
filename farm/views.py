from __future__ import annotations

import re

from django.db.models import Q, QuerySet
from drf_spectacular.utils import extend_schema, extend_schema_view, inline_serializer
from rest_framework import mixins, serializers, status, viewsets
from rest_framework.decorators import action
from rest_framework.request import Request
from rest_framework.exceptions import NotFound
from rest_framework.response import Response
from rest_framework.views import APIView

from common import query
from common.ids import ID_REGEX
from common.permissions import IsAdminOrOwnerRole, IsAdminRole, IsOwnerRole, IsRoleIn, ReadOnlyFor
from common.roles import Role, role_of
from common.schema import DATE_FROM, DATE_TO, ERROR_400, ERROR_404, query as query_param
from stats import services as stats_services
from stats.schema import PERIOD_PARAMETERS, BatchStatsSerializer, HouseholdStatsSerializer

from . import selectors, services
from .models import DailyReport, Household, MovementType, PoultryBatch, PoultryMovement, PoultryType
from .serializers import (
    BatchSerializer,
    BatchUpdateSerializer,
    DailyReportCreateSerializer,
    DailyReportSerializer,
    HouseholdBatchSerializer,
    HouseholdCreateSerializer,
    HouseholdSerializer,
    HouseholdUpdateSerializer,
    MovementCreateSerializer,
    MovementSerializer,
    OwnerBatchSerializer,
)
from .services import household_of

_HOUSEHOLD_CODE_RE = re.compile(r"^(?:XN-)?0*(\d{1,18})$", re.IGNORECASE)

HouseholdCreatedSerializer = inline_serializer(
    "HouseholdCreated", fields={"household": HouseholdSerializer(), "temp_password": serializers.CharField()}
)
HouseholdMapItemSerializer = inline_serializer(
    "HouseholdMapItem",
    fields={
        "id": serializers.IntegerField(),
        "code": serializers.CharField(),
        "name": serializers.CharField(),
        "owner_name": serializers.CharField(),
        "latitude": serializers.FloatField(),
        "longitude": serializers.FloatField(),
        "bird_count": serializers.IntegerField(),
        "is_active": serializers.BooleanField(),
    },
    many=True,
)


# VET read access to Phase 1 data (API_CONTRACT_V3 §1.2, §18.3); LOGISTICS never sees households/batches.
ReadAWOV = IsRoleIn(Role.ADMIN, Role.WAREHOUSE, Role.OWNER, Role.VET)
BatchAccess = IsRoleIn(Role.ADMIN, Role.WAREHOUSE) | ReadOnlyFor(Role.VET)
MSG_VET_DEATH_ONLY = "Veterinar faqat nobud bo‘lishni qayd eta oladi"
MSG_MY_BATCH_NOT_FOUND = "Partiya topilmadi."


def scope_to_owner(qs: QuerySet, user, household_field: str = "household") -> QuerySet:
    """OWNERs only ever see rows of their own household (others → empty list / 404)."""
    if role_of(user) == Role.OWNER:
        return qs.filter(**{f"{household_field}__owner": user})
    return qs


@extend_schema_view(
    list=extend_schema(
        parameters=[
            query_param("search", description="nom, telefon, MFY, manzil, egasi, kod (XN-0003)"),
            query_param("region"),
            query_param("is_active", bool),
        ]
    ),
    create=extend_schema(request=HouseholdCreateSerializer, responses={201: HouseholdCreatedSerializer, 400: ERROR_400}),
    partial_update=extend_schema(request=HouseholdUpdateSerializer, responses={200: HouseholdSerializer, 400: ERROR_400}),
    map=extend_schema(responses=HouseholdMapItemSerializer),
    batches=extend_schema(responses=HouseholdBatchSerializer(many=True)),
)
class HouseholdViewSet(mixins.ListModelMixin, mixins.RetrieveModelMixin, viewsets.GenericViewSet):
    serializer_class = HouseholdSerializer
    lookup_value_regex = ID_REGEX

    def get_permissions(self):
        if self.action in ("create", "partial_update"):
            return [IsAdminRole()]
        return [ReadAWOV()]

    def get_queryset(self) -> QuerySet[Household]:
        qs = Household.objects.select_related("owner")
        if role_of(self.request.user) == Role.OWNER:
            qs = qs.filter(owner=self.request.user)
        qs = selectors.households_with_counts(qs).order_by("id")
        if self.action != "list":
            return qs
        search = query.get_str(self.request, "search")
        if search:
            condition = (
                Q(name__icontains=search)
                | Q(phone__icontains=search)
                | Q(mfy__icontains=search)
                | Q(address__icontains=search)
                | Q(owner__first_name__icontains=search)
                | Q(owner__last_name__icontains=search)
                | Q(owner__username__icontains=search)
            )
            code = _HOUSEHOLD_CODE_RE.match(search)
            if code:
                condition |= Q(pk=int(code.group(1)))
            qs = qs.filter(condition)
        region = query.get_str(self.request, "region")
        if region:
            qs = qs.filter(region__iexact=region)
        is_active = query.get_bool(self.request, "is_active")
        if is_active is not None:
            qs = qs.filter(is_active=is_active)
        is_demo = query.get_bool(self.request, "is_demo")
        if is_demo is not None:
            qs = qs.filter(is_demo=is_demo)
        return qs

    def _fresh(self, household: Household) -> dict:
        household = self.get_queryset().get(pk=household.pk)
        return HouseholdSerializer(household).data

    def create(self, request: Request) -> Response:
        serializer = HouseholdCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        household, password = services.create_household(
            services.HouseholdInput(**serializer.validated_data), actor=request.user
        )
        return Response(
            {"household": self._fresh(household), "temp_password": password},
            status=status.HTTP_201_CREATED,
        )

    def partial_update(self, request: Request, pk: str | None = None) -> Response:
        household = self.get_object()
        serializer = HouseholdUpdateSerializer(household, data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        services.update_household(household, dict(serializer.validated_data), actor=request.user)
        return Response(self._fresh(household))

    @action(detail=False, methods=["get"], url_path="map", pagination_class=None)
    def map(self, request: Request) -> Response:
        qs = self.get_queryset().filter(latitude__isnull=False, longitude__isnull=False)
        data = [
            {
                "id": h.pk,
                "code": h.code,
                "name": h.name,
                "owner_name": h.owner.full_name,
                "latitude": float(h.latitude),
                "longitude": float(h.longitude),
                "bird_count": h.bird_count,
                "is_active": h.is_active,
            }
            for h in qs
        ]
        return Response(data)

    @action(detail=True, methods=["get"], url_path="batches", pagination_class=None)
    def batches(self, request: Request, pk: str | None = None) -> Response:
        household = self.get_object()
        rows = selectors.household_batches(household.pk)
        return Response(HouseholdBatchSerializer(rows, many=True).data)

    @extend_schema(parameters=PERIOD_PARAMETERS, responses=HouseholdStatsSerializer)
    @action(detail=True, methods=["get"], url_path="stats", pagination_class=None)
    def stats(self, request: Request, pk: str | None = None) -> Response:
        """Household statistics (API_CONTRACT_V2 §1.5); OWNER: own household only (others → 404)."""
        household = self.get_object()
        return Response(stats_services.household_stats(household, request.query_params))


@extend_schema_view(
    list=extend_schema(
        parameters=[
            query_param("search", description="kod, zot, yetkazib beruvchi"),
            query_param("poultry_type", enum=PoultryType.values),
        ]
    ),
)
class BatchViewSet(mixins.ListModelMixin, mixins.RetrieveModelMixin, viewsets.GenericViewSet):
    permission_classes = [BatchAccess]
    serializer_class = BatchSerializer
    lookup_value_regex = ID_REGEX

    def get_queryset(self) -> QuerySet[PoultryBatch]:
        qs = selectors.batches_with_counts().order_by("-arrived_at", "-id")
        if self.action != "list":
            return qs
        search = query.get_str(self.request, "search")
        if search:
            qs = qs.filter(
                Q(code__icontains=search) | Q(breed__icontains=search) | Q(supplier__icontains=search)
            )
        poultry_type = query.get_choice(self.request, "poultry_type", PoultryType)
        if poultry_type:
            qs = qs.filter(poultry_type=poultry_type)
        return qs

    def create(self, request: Request) -> Response:
        serializer = BatchSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        batch = services.create_batch(actor=request.user, **serializer.validated_data)
        batch = self.get_queryset().get(pk=batch.pk)
        return Response(BatchSerializer(batch).data, status=status.HTTP_201_CREATED)

    @extend_schema(request=BatchUpdateSerializer, responses=BatchSerializer)
    def partial_update(self, request: Request, pk: str | None = None) -> Response:
        """Only ``health_status`` and ``supplier`` can change (API_CONTRACT_V2 §4)."""
        batch = self.get_object()
        serializer = BatchUpdateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        services.update_batch(actor=request.user, batch_id=batch.pk, changes=dict(serializer.validated_data))
        return Response(BatchSerializer(self.get_queryset().get(pk=batch.pk)).data)

    @extend_schema(parameters=PERIOD_PARAMETERS, responses=BatchStatsSerializer)
    @action(detail=True, methods=["get"], url_path="stats", pagination_class=None)
    def stats(self, request: Request, pk: str | None = None) -> Response:
        """Batch statistics (API_CONTRACT_V2 §1.6); default period = the whole life (max 366 days)."""
        batch = self.get_object()
        return Response(
            stats_services.batch_stats(batch, BatchSerializer(batch).data, request.query_params)
        )


@extend_schema_view(
    list=extend_schema(
        parameters=[
            query_param("batch", int),
            query_param("household", int),
            query_param("type", enum=MovementType.values),
            DATE_FROM,
            DATE_TO,
        ]
    ),
    create=extend_schema(request=MovementCreateSerializer, responses={201: MovementSerializer, 400: ERROR_400}),
)
class MovementViewSet(mixins.ListModelMixin, viewsets.GenericViewSet):
    permission_classes = [ReadAWOV]
    serializer_class = MovementSerializer

    def get_queryset(self) -> QuerySet[PoultryMovement]:
        qs = PoultryMovement.objects.select_related("batch", "household", "created_by")
        qs = scope_to_owner(qs, self.request.user).order_by("-date", "-id")
        batch = query.get_int(self.request, "batch")
        if batch is not None:
            qs = qs.filter(batch_id=batch)
        household = query.get_int(self.request, "household")
        if household is not None:
            qs = qs.filter(household_id=household)
        movement_type = query.get_choice(self.request, "type", MovementType)
        if movement_type:
            qs = qs.filter(type=movement_type)
        return query.apply_date_range(qs, self.request, "date")

    def create(self, request: Request) -> Response:
        serializer = MovementCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        if role_of(request.user) == Role.VET:  # V3 §8.6: DEATH only, household required
            if data["type"] != MovementType.DEATH:
                raise serializers.ValidationError({"type": [MSG_VET_DEATH_ONLY]})
            if data["household"] is None:
                raise serializers.ValidationError({"household": ["Xonadonni tanlang"]})
        movement = services.record_movement(
            actor=request.user,
            batch=data["batch"],
            household=data["household"],
            movement_type=data["type"],
            quantity=data["quantity"],
            on_date=data["date"],
            note=data["note"],
        )
        return Response(MovementSerializer(movement).data, status=status.HTTP_201_CREATED)


@extend_schema_view(
    list=extend_schema(parameters=[query_param("household", int), DATE_FROM, DATE_TO]),
    create=extend_schema(request=DailyReportCreateSerializer, responses={201: DailyReportSerializer, 400: ERROR_400}),
)
class DailyReportViewSet(mixins.ListModelMixin, viewsets.GenericViewSet):
    serializer_class = DailyReportSerializer

    def get_permissions(self):
        if self.action == "create":
            return [IsAdminOrOwnerRole()]
        return [ReadAWOV()]

    def get_queryset(self) -> QuerySet[DailyReport]:
        qs = DailyReport.objects.select_related("household", "created_by")
        qs = scope_to_owner(qs, self.request.user).order_by("-date", "-id")
        household = query.get_int(self.request, "household")
        if household is not None:
            qs = qs.filter(household_id=household)
        return query.apply_date_range(qs, self.request, "date")

    def create(self, request: Request) -> Response:
        serializer = DailyReportCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        report = services.create_daily_report(
            actor=request.user,
            household=data["household"],
            batch=data["batch"],
            on_date=data["date"],
            feed_kg=data["feed_kg"],
            deaths=data["deaths"],
            meat_kg=data["meat_kg"],
            avg_weight_g=data["avg_weight_g"],
            note=data["note"],
        )
        return Response(DailyReportSerializer(report).data, status=status.HTTP_201_CREATED)


class MyBatchDetailView(APIView):
    """``GET /api/my/batches/{id}`` — OWNER: a batch with birds in the own household (other batches → 404).

    TZ §2 ROLE 6 "Parrandalar partiyasi haqida ma’lumot olish" / §6: age, supplier, health status and the
    vaccination history of this batch in the owner's household.
    """

    permission_classes = [IsOwnerRole]

    @extend_schema(responses={200: OwnerBatchSerializer, 404: ERROR_404})
    def get(self, request: Request, pk: int) -> Response:
        from django.utils import timezone

        from warehouse.readiness import optional_model

        household = household_of(request.user)
        row = next((item for item in selectors.household_batches(household.pk) if item["batch_id"] == pk), None)
        if row is None:
            raise NotFound(MSG_MY_BATCH_NOT_FOUND)
        batch = PoultryBatch.objects.get(pk=pk)
        vaccinations = []
        model = optional_model("vet", "Vaccination", "batch", "household", "planned_date", "done_date")
        if model is not None:
            for item in model.objects.filter(household=household, batch_id=pk).order_by("-planned_date", "-id")[:50]:
                vaccinations.append(
                    {
                        "id": item.pk,
                        "code": item.code,
                        "vaccine_name": item.vaccine_name,
                        "status": item.status,
                        "status_display": item.get_status_display(),
                        "planned_date": item.planned_date,
                        "done_date": item.done_date,
                        "dose": item.dose,
                    }
                )
        today = timezone.localdate()
        data = {
            "id": batch.pk,
            "code": batch.code,
            "poultry_type": batch.poultry_type,
            "poultry_type_display": batch.get_poultry_type_display(),
            "breed": batch.breed,
            "supplier": batch.supplier,
            "arrived_at": batch.arrived_at,
            "age_days_at_arrival": batch.age_days,
            "age_days": batch.age_days + max((today - batch.arrived_at).days, 0),
            "health_status": batch.health_status,
            "count": row["count"],
            "first_received_date": row["first_received_date"],
            "vaccinations": vaccinations,
        }
        return Response(OwnerBatchSerializer(data).data)

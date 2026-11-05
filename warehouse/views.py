from __future__ import annotations

from django.db.models import Count, F, Prefetch, Q, QuerySet
from django.shortcuts import get_object_or_404
from django.utils import timezone
from drf_spectacular.utils import OpenApiResponse, extend_schema, extend_schema_view
from rest_framework import mixins, status, viewsets
from rest_framework.decorators import action
from rest_framework.request import Request
from rest_framework.response import Response

from common import query
from common.ids import ID_REGEX
from common.permissions import IsAnyRole, IsRoleIn, IsStaffRole, ReadOnlyFor
from common.roles import Role, role_of
from common.schema import DATE_FROM, DATE_TO, ERROR_400, query as query_param
from stats import services as stats_services
from stats.schema import PERIOD_PARAMETERS, ProductStatsSerializer

from farm.models import PoultryType

from . import inventory, norms, services
from . import lots as lot_reads
from .models import (
    ConstructionOption,
    FeedNorm,
    InventoryCount,
    InventoryCountLine,
    InventoryCountStatus,
    Product,
    ProductCategory,
    StockLot,
    StockTransaction,
    StockTransactionType,
    returned_task_out,
)
from .serializers import (
    ConstructionOptionSerializer,
    FeedNormSerializer,
    FeedNormWriteSerializer,
    InventoryCancelSerializer,
    InventoryCountCreateSerializer,
    InventoryCountSerializer,
    InventoryCountShortSerializer,
    InventoryLinesSerializer,
    ProductSerializer,
    StockLotSerializer,
    StockTransactionCreateSerializer,
    StockTransactionSerializer,
    lots_ready,
    stock_files_map,
    task_order_ids,
    with_lots,
)

#: V3 §1.2: products A, W (write) + V (read); lots A, W, V (read); inventory A, W.
ProductAccess = IsRoleIn(Role.ADMIN, Role.WAREHOUSE) | ReadOnlyFor(Role.VET)
LotAccess = IsRoleIn(Role.ADMIN, Role.WAREHOUSE, Role.VET)
StaffOnly = IsRoleIn(Role.ADMIN, Role.WAREHOUSE)
AdminOnly = IsRoleIn(Role.ADMIN)

ERROR_409 = OpenApiResponse(description="Holat mos emas (masalan, tasdiqlangan inventarizatsiya)")

LOT_PARAMETERS = [
    query_param("expiring_within", int, "0–365: qoldig‘i bor va muddati bugun + N kungacha (o‘tganlari ham)"),
    query_param("expired", bool, "1 — faqat muddati o‘tgan, qoldig‘i bor partiyalar"),
    query_param("has_remaining", bool, "1 — faqat qoldig‘i bor partiyalar"),
]


def _lot_filters(request: Request) -> dict:
    within = query.get_int(request, "expiring_within")
    if within is not None and not 0 <= within <= lot_reads.MAX_EXPIRING_WITHIN:
        from rest_framework.exceptions import ValidationError

        raise ValidationError({"expiring_within": ["0 dan 365 gacha son kiriting."]})
    return {
        "expiring_within": within,
        "expired": query.get_bool(request, "expired"),
        "has_remaining": query.get_bool(request, "has_remaining"),
    }


@extend_schema_view(
    list=extend_schema(
        parameters=[
            query_param("search"),
            query_param("category", enum=ProductCategory.values),
            query_param("low", bool, "1 — faqat minimal zaxiradan kam qolganlar"),
            query_param("is_active", bool),
        ]
    ),
    create=extend_schema(request=ProductSerializer, responses={201: ProductSerializer, 400: ERROR_400}),
    partial_update=extend_schema(request=ProductSerializer, responses={200: ProductSerializer, 400: ERROR_400}),
)
class ProductViewSet(mixins.ListModelMixin, mixins.RetrieveModelMixin, viewsets.GenericViewSet):
    serializer_class = ProductSerializer
    lookup_value_regex = ID_REGEX

    def get_permissions(self):
        if self.action == "stats":
            return [IsStaffRole()]
        if self.action == "lots":
            return [LotAccess()]
        return [ProductAccess()]

    def get_queryset(self) -> QuerySet[Product]:
        qs = Product.objects.order_by("name", "id")
        if lots_ready():
            qs = qs.annotate(nearest_expiry=lot_reads.nearest_expiry_subquery())
        if self.action != "list":
            return qs
        search = query.get_str(self.request, "search")
        if search:
            qs = qs.filter(name__icontains=search)
        category = query.get_choice(self.request, "category", ProductCategory)
        if category:
            qs = qs.filter(category=category)
        if query.get_bool(self.request, "low"):
            qs = qs.filter(balance__lte=F("min_stock"))
        is_active = query.get_bool(self.request, "is_active")
        if is_active is not None:
            qs = qs.filter(is_active=is_active)
        return qs

    def create(self, request: Request) -> Response:
        serializer = ProductSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        product = services.create_product(actor=request.user, data=dict(serializer.validated_data))
        return Response(ProductSerializer(product).data, status=status.HTTP_201_CREATED)

    def partial_update(self, request: Request, pk: str | None = None) -> Response:
        product = self.get_object()
        serializer = ProductSerializer(product, data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        product = services.update_product(
            actor=request.user, product_id=product.pk, changes=dict(serializer.validated_data)
        )
        return Response(ProductSerializer(product).data)

    @extend_schema(parameters=PERIOD_PARAMETERS, responses=ProductStatsSerializer)
    @action(detail=True, methods=["get"], url_path="stats", pagination_class=None)
    def stats(self, request: Request, pk: str | None = None) -> Response:
        """Product statistics (API_CONTRACT_V2 §1.7), also for inactive products."""
        product = self.get_object()
        return Response(
            stats_services.product_stats(product, ProductSerializer(product).data, request.query_params)
        )

    @extend_schema(parameters=LOT_PARAMETERS, responses=StockLotSerializer(many=True))
    @action(detail=True, methods=["get"], url_path="lots")
    def lots(self, request: Request, pk: str | None = None) -> Response:
        """Lots of one product (V3 §5.2), same filters and order as ``/api/stock-lots``."""
        product = get_object_or_404(Product.objects.only("pk"), pk=pk)
        today = timezone.localdate()
        qs = lot_reads.filter_lots(
            lot_reads.lots_queryset(), today=today, product=product.pk, **_lot_filters(request)
        )
        page = self.paginate_queryset(qs)
        context = {"request": request, "today": today}
        return self.get_paginated_response(StockLotSerializer(page, many=True, context=context).data)


@extend_schema_view(
    list=extend_schema(
        parameters=[
            query_param("product", int),
            query_param("type", enum=StockTransactionType.values),
            query_param("household", int),
            DATE_FROM,
            DATE_TO,
        ]
    ),
    create=extend_schema(
        request=StockTransactionCreateSerializer, responses={201: StockTransactionSerializer, 400: ERROR_400}
    ),
)
class StockTransactionViewSet(mixins.ListModelMixin, viewsets.GenericViewSet):
    serializer_class = StockTransactionSerializer

    def get_permissions(self):
        if self.action == "create":
            return [IsStaffRole()]
        return [IsAnyRole()]

    def get_queryset(self) -> QuerySet[StockTransaction]:
        qs = with_lots(StockTransaction.objects.select_related("product", "household", "created_by"))
        if role_of(self.request.user) == Role.OWNER:
            # Goods of a task cancelled in transit never reached the household.
            qs = qs.filter(type=StockTransactionType.OUT, household__owner=self.request.user).exclude(
                returned_task_out()
            )
        qs = qs.order_by("-created_at", "-id")
        product = query.get_int(self.request, "product")
        if product is not None:
            qs = qs.filter(product_id=product)
        tx_type = query.get_choice(self.request, "type", StockTransactionType)
        if tx_type:
            qs = qs.filter(type=tx_type)
        household = query.get_int(self.request, "household")
        if household is not None:
            qs = qs.filter(household_id=household)
        return query.apply_date_range(qs, self.request, "created_at__date")

    def list(self, request: Request, *args, **kwargs) -> Response:
        page = self.paginate_queryset(self.filter_queryset(self.get_queryset()))
        context = self.get_serializer_context()
        context["files_map"] = stock_files_map(page)
        if role_of(request.user) == Role.OWNER:
            context["task_orders"] = task_order_ids(page)
        return self.get_paginated_response(StockTransactionSerializer(page, many=True, context=context).data)

    def create(self, request: Request) -> Response:
        serializer = StockTransactionCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        row = services.record_stock_transaction(
            actor=request.user,
            product_id=data["product"].pk,
            transaction_type=data["type"],
            quantity=data["quantity"],
            document_no=data["document_no"],
            household=data["household"],
            note=data["note"],
            lot_no=data["lot_no"],
            expiry_date=data["expiry_date"],
            lot_id=data["lot"],
            file_ids=data["file_ids"],
        )
        row = with_lots(StockTransaction.objects.select_related("product", "household", "created_by")).get(pk=row.pk)
        return Response(
            StockTransactionSerializer(row, context=self.get_serializer_context()).data,
            status=status.HTTP_201_CREATED,
        )


@extend_schema_view(
    list=extend_schema(parameters=[query_param("product", int), *LOT_PARAMETERS]),
)
class StockLotViewSet(mixins.ListModelMixin, viewsets.GenericViewSet):
    """``GET /api/stock-lots`` (A, W, V read), V3 §5.2."""

    serializer_class = StockLotSerializer
    permission_classes = [LotAccess]

    def get_queryset(self) -> QuerySet[StockLot]:
        return lot_reads.lots_queryset()

    def list(self, request: Request, *args, **kwargs) -> Response:
        today = timezone.localdate()
        qs = lot_reads.filter_lots(
            self.get_queryset(), today=today, product=query.get_int(request, "product"), **_lot_filters(request)
        )
        page = self.paginate_queryset(qs)
        context = {"request": request, "today": today}
        return self.get_paginated_response(StockLotSerializer(page, many=True, context=context).data)


def _counts_queryset() -> QuerySet[InventoryCount]:
    counted = Q(lines__counted_qty__isnull=False)
    differs = Q(status=InventoryCountStatus.APPROVED) & ~Q(lines__difference=0) & Q(lines__difference__isnull=False)
    differs |= (
        Q(status=InventoryCountStatus.DRAFT) & counted & ~Q(lines__counted_qty=F("lines__product__balance"))
    )
    return (
        InventoryCount.objects.select_related("created_by")
        .annotate(
            lines_total=Count("lines", distinct=True),
            lines_counted=Count("lines", filter=counted, distinct=True),
            lines_with_difference=Count("lines", filter=differs, distinct=True),
        )
        .order_by("-created_at", "-id")
    )


def _count_detail(pk: int) -> InventoryCount:
    lines = InventoryCountLine.objects.select_related("product", "adjustment").order_by("product__name", "id")
    return get_object_or_404(
        InventoryCount.objects.select_related("created_by", "approved_by", "cancelled_by").prefetch_related(
            Prefetch("lines", queryset=lines)
        ),
        pk=pk,
    )


@extend_schema_view(
    list=extend_schema(parameters=[query_param("status", enum=InventoryCountStatus.values)]),
    retrieve=extend_schema(responses=InventoryCountSerializer),
)
class InventoryCountViewSet(mixins.ListModelMixin, viewsets.GenericViewSet):
    """Inventory counts (V3 §5.3): A, W; approve A only."""

    serializer_class = InventoryCountShortSerializer
    lookup_value_regex = ID_REGEX

    def get_permissions(self):
        if self.action == "approve":
            return [AdminOnly()]
        return [StaffOnly()]

    def get_queryset(self) -> QuerySet[InventoryCount]:
        qs = _counts_queryset()
        status_value = query.get_choice(self.request, "status", InventoryCountStatus)
        if status_value:
            qs = qs.filter(status=status_value)
        return qs

    def _detail(self, pk) -> Response:
        return Response(InventoryCountSerializer(_count_detail(pk), context=self.get_serializer_context()).data)

    def retrieve(self, request: Request, pk: str | None = None) -> Response:
        return self._detail(pk)

    @extend_schema(request=InventoryCountCreateSerializer, responses={201: InventoryCountSerializer, 400: ERROR_400})
    def create(self, request: Request) -> Response:
        serializer = InventoryCountCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        count = inventory.create_count(
            actor=request.user, note=data["note"], category=data["category"], product_ids=data["product_ids"]
        )
        response = self._detail(count.pk)
        response.status_code = status.HTTP_201_CREATED
        return response

    @extend_schema(
        request=InventoryLinesSerializer, responses={200: InventoryCountSerializer, 400: ERROR_400, 409: ERROR_409}
    )
    @action(detail=True, methods=["patch"], url_path="lines")
    def lines(self, request: Request, pk: str | None = None) -> Response:
        get_object_or_404(InventoryCount.objects.only("pk"), pk=pk)
        serializer = InventoryLinesSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        inventory.update_lines(actor=request.user, count_id=int(pk), lines=serializer.validated_data["lines"])
        return self._detail(pk)

    @extend_schema(request=None, responses={200: InventoryCountSerializer, 400: ERROR_400, 409: ERROR_409})
    @action(detail=True, methods=["post"], url_path="approve")
    def approve(self, request: Request, pk: str | None = None) -> Response:
        get_object_or_404(InventoryCount.objects.only("pk"), pk=pk)
        inventory.approve(actor=request.user, count_id=int(pk))
        return self._detail(pk)

    @extend_schema(request=InventoryCancelSerializer, responses={200: InventoryCountSerializer, 409: ERROR_409})
    @action(detail=True, methods=["post"], url_path="cancel")
    def cancel(self, request: Request, pk: str | None = None) -> Response:
        get_object_or_404(InventoryCount.objects.only("pk"), pk=pk)
        serializer = InventoryCancelSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        inventory.cancel(actor=request.user, count_id=int(pk), reason=serializer.validated_data["reason"])
        return self._detail(pk)


# --- V4 §5 feed norms and construction options ---------------------------------------------------

#: Read A, W, V; write ADMIN (Super Admin included).
NormAccess = IsRoleIn(Role.ADMIN) | ReadOnlyFor(Role.WAREHOUSE, Role.VET)


@extend_schema_view(
    list=extend_schema(parameters=[query_param("poultry_type", enum=PoultryType.values)]),
    create=extend_schema(request=FeedNormWriteSerializer, responses={201: FeedNormSerializer, 400: ERROR_400}),
    partial_update=extend_schema(request=FeedNormWriteSerializer, responses={200: FeedNormSerializer, 400: ERROR_400}),
    destroy=extend_schema(responses={204: None}),
)
class FeedNormViewSet(
    mixins.ListModelMixin, mixins.RetrieveModelMixin, mixins.DestroyModelMixin, viewsets.GenericViewSet
):
    """``/api/feed-norms`` (V4 §5): grams of feed per bird per day by age range."""

    serializer_class = FeedNormSerializer
    permission_classes = [NormAccess]
    lookup_value_regex = ID_REGEX
    http_method_names = ["get", "post", "patch", "delete", "head", "options"]

    def get_queryset(self) -> QuerySet[FeedNorm]:
        qs = FeedNorm.objects.select_related("product").order_by("poultry_type", "day_from", "id")
        poultry_type = query.get_choice(self.request, "poultry_type", PoultryType)
        if poultry_type:
            qs = qs.filter(poultry_type=poultry_type)
        return qs

    def create(self, request: Request) -> Response:
        serializer = FeedNormWriteSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        norm = norms.create_feed_norm(actor=request.user, data=dict(serializer.validated_data))
        return Response(FeedNormSerializer(norm).data, status=status.HTTP_201_CREATED)

    def partial_update(self, request: Request, pk: str | None = None) -> Response:
        norm = self.get_object()
        serializer = FeedNormWriteSerializer(data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        norm = norms.update_feed_norm(actor=request.user, norm_id=norm.pk, changes=dict(serializer.validated_data))
        return Response(FeedNormSerializer(norm).data)

    def destroy(self, request: Request, pk: str | None = None) -> Response:
        norm = self.get_object()
        norms.delete_feed_norm(actor=request.user, norm_id=norm.pk)
        return Response(status=status.HTTP_204_NO_CONTENT)


@extend_schema_view(
    list=extend_schema(parameters=[query_param("search"), query_param("is_active", bool)]),
    create=extend_schema(
        request=ConstructionOptionSerializer, responses={201: ConstructionOptionSerializer, 400: ERROR_400}
    ),
    partial_update=extend_schema(
        request=ConstructionOptionSerializer, responses={200: ConstructionOptionSerializer, 400: ERROR_400}
    ),
    destroy=extend_schema(responses={204: None}),
)
class ConstructionOptionViewSet(
    mixins.ListModelMixin, mixins.RetrieveModelMixin, mixins.DestroyModelMixin, viewsets.GenericViewSet
):
    """``/api/construction-options`` (V4 §5): construction catalogue."""

    serializer_class = ConstructionOptionSerializer
    permission_classes = [NormAccess]
    lookup_value_regex = ID_REGEX
    http_method_names = ["get", "post", "patch", "delete", "head", "options"]

    def get_queryset(self) -> QuerySet[ConstructionOption]:
        qs = ConstructionOption.objects.order_by("name", "id")
        search = query.get_str(self.request, "search")
        if search:
            qs = qs.filter(name__icontains=search)
        is_active = query.get_bool(self.request, "is_active")
        if is_active is not None:
            qs = qs.filter(is_active=is_active)
        return qs

    def create(self, request: Request) -> Response:
        serializer = ConstructionOptionSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        option = norms.create_construction_option(actor=request.user, data=dict(serializer.validated_data))
        return Response(ConstructionOptionSerializer(option).data, status=status.HTTP_201_CREATED)

    def partial_update(self, request: Request, pk: str | None = None) -> Response:
        option = self.get_object()
        serializer = ConstructionOptionSerializer(option, data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        option = norms.update_construction_option(
            actor=request.user, option_id=option.pk, changes=dict(serializer.validated_data)
        )
        return Response(ConstructionOptionSerializer(option).data)

    def destroy(self, request: Request, pk: str | None = None) -> Response:
        option = self.get_object()
        norms.delete_construction_option(actor=request.user, option_id=option.pk)
        return Response(status=status.HTTP_204_NO_CONTENT)

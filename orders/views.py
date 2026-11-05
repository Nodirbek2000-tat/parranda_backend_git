"""API views of the orders app (API_CONTRACT_V3 §6.4, §5.5): parse/validate input, call ``services``, serialize.

Access (§1.2): ``/api/orders*`` — ADMIN all actions, WAREHOUSE read, OWNER own household (create, cancel / delete NEW;
other households → ``404``); LOGISTICS / VET → ``403``. ``/api/catalog/products`` — ADMIN, WAREHOUSE, OWNER.
"""

from __future__ import annotations

from django.db.models import BooleanField, Case, F, IntegerField, Prefetch, Q, Value, When
from drf_spectacular.utils import OpenApiParameter, OpenApiResponse, extend_schema
from rest_framework import status
from rest_framework.exceptions import NotFound, ValidationError
from rest_framework.generics import GenericAPIView
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView

from common import query
from common.periods import day_range
from common.permissions import IsAdminRole, IsRoleIn, ReadOnlyFor
from common.roles import Role, role_of
from common.schema import DATE_FROM, DATE_TO, ERROR_400, ERROR_403, ERROR_404, query as query_param
from core import search as core_search
from warehouse.models import Product, ProductCategory

from . import bridge, selectors, services
from .models import Order, OrderItem, OrderKind, OrderStatus, OrderStatusLog
from .serializers import (
    CatalogProductSerializer,
    OrderApproveSerializer,
    OrderCreateSerializer,
    OrderDetailSerializer,
    OrderSerializer,
    ReasonSerializer,
    TaskDataSerializer,
)

MSG_NOT_FOUND = "Buyurtma topilmadi."
ID_PATH = OpenApiParameter("pk", int, OpenApiParameter.PATH)
ERROR_409 = OpenApiResponse(description="Buyurtma holati amalga ruxsat bermaydi: {\"detail\": \"...\"}")

A, W, O = Role.ADMIN, Role.WAREHOUSE, Role.OWNER


def _items_prefetch() -> Prefetch:
    return Prefetch("items", queryset=OrderItem.objects.select_related("product").order_by("id"))


def _base_queryset(user):
    return selectors.scoped_orders(user).select_related("household", "household__owner", "created_by", "decided_by")


def _get_order(request: Request, pk: int, *, detail: bool = False) -> Order:
    qs = _base_queryset(request.user).prefetch_related(_items_prefetch())
    if detail:
        qs = qs.prefetch_related(
            Prefetch("status_logs", queryset=OrderStatusLog.objects.select_related("actor").order_by("created_at", "id"))
        )
    order = qs.filter(pk=pk).first()
    if order is None:
        raise NotFound(MSG_NOT_FOUND)
    return order


def _context(request: Request, orders: list[Order]) -> dict:
    tasks = bridge.active_tasks(order.pk for order in orders)
    ready = bridge.available() if role_of(request.user) == Role.ADMIN else False
    return {"request": request, "tasks": tasks, "logistics_ready": ready}


def order_response(request: Request, order_id: int, *, code: int = status.HTTP_200_OK) -> Response:
    order = _get_order(request, order_id, detail=True)
    data = OrderDetailSerializer(order, context=_context(request, [order])).data
    return Response(data, status=code)


def _statuses(request: Request) -> list[str]:
    raw = query.get_str(request, "status")
    values = [value.strip() for value in raw.split(",") if value.strip()]
    for value in values:
        if value not in OrderStatus.values:
            raise ValidationError({"status": [f"“{value}” ruxsat etilgan qiymat emas."]})
    return values


class OrderListCreateView(GenericAPIView):
    """``GET /api/orders`` (A, W all; O own) · ``POST /api/orders`` (O own household, A any household)."""

    permission_classes = [IsRoleIn(A, O) | ReadOnlyFor(W)]
    serializer_class = OrderSerializer

    def get_queryset(self):
        request = self.request
        qs = _base_queryset(request.user).prefetch_related(_items_prefetch()).order_by("-created_at", "-id")
        statuses = _statuses(request)
        if statuses:
            qs = qs.filter(status__in=statuses)
        kind = query.get_choice(request, "kind", OrderKind)
        if kind:
            qs = qs.filter(kind=kind)
        household = query.get_int(request, "household")
        if household is not None:
            qs = qs.filter(household_id=household)
        date_from, date_to = query.get_date(request, "date_from"), query.get_date(request, "date_to")
        if date_from and date_to and date_from > date_to:
            raise ValidationError({"date_to": ["Tugash sanasi boshlanish sanasidan oldin bo‘lishi mumkin emas."]})
        start, end = day_range(date_from, date_to)
        if start is not None:
            qs = qs.filter(created_at__gte=start)
        if end is not None:
            qs = qs.filter(created_at__lt=end)
        search = query.get_str(request, "search")
        if search:
            condition = Q(household__name__icontains=search)
            for prefix, field in (("BY", "pk"), ("XN", "household_id")):
                pk = core_search.code_pk(search, prefix)
                if pk is not None:
                    condition |= Q(**{field: pk})
            qs = qs.filter(condition)
        return qs

    @extend_schema(
        parameters=[
            query_param("status", description="vergul bilan: NEW,APPROVED,..."),
            query_param("kind", enum=OrderKind.values),
            query_param("household", int),
            DATE_FROM,
            DATE_TO,
            query_param("search", description="buyurtma kodi, xonadon nomi yoki kodi"),
        ],
        responses={200: OrderSerializer(many=True), 403: ERROR_403},
    )
    def get(self, request: Request) -> Response:
        page = self.paginate_queryset(self.get_queryset())
        context = _context(request, page)
        return self.get_paginated_response(OrderSerializer(page, many=True, context=context).data)

    @extend_schema(request=OrderCreateSerializer, responses={201: OrderDetailSerializer, 400: ERROR_400, 403: ERROR_403})
    def post(self, request: Request) -> Response:
        payload = request.data
        if role_of(request.user) == Role.OWNER and hasattr(payload, "get") and "household" in payload:
            # OWNER: household is forced to the own one, so the sent id is not even looked up
            # (no 400/201 difference that would reveal whether another household id exists).
            payload = {key: value for key, value in payload.items() if key != "household"}
        serializer = OrderCreateSerializer(data=payload)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        order = services.create_order(
            actor=request.user,
            kind=data["kind"],
            items=data["items"],
            household=data["household"],
            note=data["note"],
            desired_date=data["desired_date"],
        )
        return order_response(request, order.pk, code=status.HTTP_201_CREATED)


class OrderDetailView(APIView):
    """``GET /api/orders/{id}`` with ``history`` (A, W; O own → others 404) · ``DELETE`` (A; O own NEW order)."""

    def get_permissions(self):
        if self.request.method == "DELETE":
            return [IsRoleIn(A, O)()]
        return [IsRoleIn(A, W, O)()]

    @extend_schema(parameters=[ID_PATH], responses={200: OrderDetailSerializer, 403: ERROR_403, 404: ERROR_404})
    def get(self, request: Request, pk: int) -> Response:
        return order_response(request, pk)

    @extend_schema(parameters=[ID_PATH], request=None, responses={204: None, 403: ERROR_403, 404: ERROR_404, 409: ERROR_409})
    def delete(self, request: Request, pk: int) -> Response:
        """Soft delete (hidden everywhere, history kept); see ``services.delete_order`` for the ``409`` rules."""
        _get_order(request, pk)  # OWNER: another household's order → 404; deleted → 404
        services.delete_order(actor=request.user, order_id=pk)
        return Response(status=status.HTTP_204_NO_CONTENT)


class OrderApproveView(APIView):
    permission_classes = [IsAdminRole]

    @extend_schema(
        parameters=[ID_PATH],
        request=OrderApproveSerializer,
        responses={200: OrderDetailSerializer, 400: ERROR_400, 404: ERROR_404, 409: ERROR_409},
    )
    def post(self, request: Request, pk: int) -> Response:
        _get_order(request, pk)
        serializer = OrderApproveSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        services.approve_order(
            actor=request.user,
            order_id=pk,
            items=list(data["items"]),
            decision_note=data["decision_note"],
            create_task=data["create_task"],
            task_data=dict(data["task"] or {}),
        )
        return order_response(request, pk)


class OrderRejectView(APIView):
    permission_classes = [IsAdminRole]

    @extend_schema(
        parameters=[ID_PATH],
        request=ReasonSerializer,
        responses={200: OrderDetailSerializer, 400: ERROR_400, 404: ERROR_404, 409: ERROR_409},
    )
    def post(self, request: Request, pk: int) -> Response:
        _get_order(request, pk)
        serializer = ReasonSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        services.reject_order(actor=request.user, order_id=pk, reason=serializer.validated_data["reason"])
        return order_response(request, pk)


class OrderCancelView(APIView):
    """OWNER: own NEW order, reason optional (≤ 500). ADMIN: NEW / APPROVED, reason 3–500."""

    permission_classes = [IsRoleIn(A, O)]

    @extend_schema(
        parameters=[ID_PATH],
        request=ReasonSerializer,
        responses={200: OrderDetailSerializer, 400: ERROR_400, 404: ERROR_404, 409: ERROR_409},
    )
    def post(self, request: Request, pk: int) -> Response:
        _get_order(request, pk)  # OWNER: another household's order → 404
        serializer = ReasonSerializer(data=request.data, optional=role_of(request.user) == Role.OWNER)
        serializer.is_valid(raise_exception=True)
        services.cancel_order(actor=request.user, order_id=pk, reason=serializer.validated_data["reason"])
        return order_response(request, pk)


class OrderCreateTaskView(APIView):
    """``POST /api/orders/{id}/create-task`` (A): APPROVED order without an active task → ``201 LogisticsTask``."""

    permission_classes = [IsAdminRole]

    @extend_schema(
        parameters=[ID_PATH],
        request=TaskDataSerializer,
        responses={201: OpenApiResponse(description="LogisticsTask (API_CONTRACT_V3 §7.3)"), 400: ERROR_400,
                   404: ERROR_404, 409: ERROR_409},
    )
    def post(self, request: Request, pk: int) -> Response:
        _get_order(request, pk)
        serializer = TaskDataSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        task = services.create_task_for_order(actor=request.user, order_id=pk, data=dict(serializer.validated_data))
        return Response(bridge.serialize_task(task, request), status=status.HTTP_201_CREATED)


class CatalogProductsView(APIView):
    """``GET /api/catalog/products?search=&category=`` (A, W, O) — **[array]** of active products,
    ordered by the category enum order, then name. Balances are not exposed: ``in_stock = available > 0``."""

    permission_classes = [IsRoleIn(A, W, O)]

    @extend_schema(
        parameters=[query_param("search"), query_param("category", enum=ProductCategory.values)],
        responses={200: CatalogProductSerializer(many=True), 403: ERROR_403},
    )
    def get(self, request: Request) -> Response:
        category_order = Case(
            *[When(category=value, then=Value(index)) for index, value in enumerate(ProductCategory.values)],
            default=Value(len(ProductCategory.values)),
            output_field=IntegerField(),
        )
        qs = (
            Product.objects.filter(is_active=True)
            .annotate(
                category_order=category_order,
                in_stock=Case(
                    When(balance__gt=F("reserved"), then=Value(True)), default=Value(False), output_field=BooleanField()
                ),
            )
            .order_by("category_order", "name", "id")
        )
        search = query.get_str(request, "search")
        if search:
            qs = qs.filter(name__icontains=search)
        category = query.get_choice(request, "category", ProductCategory)
        if category:
            qs = qs.filter(category=category)
        return Response(CatalogProductSerializer(qs, many=True).data)

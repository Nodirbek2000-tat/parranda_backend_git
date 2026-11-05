from __future__ import annotations

import ipaddress

from django.db.models import Q, QuerySet
from django.utils import timezone
from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import OpenApiParameter, extend_schema, extend_schema_view
from rest_framework import mixins, viewsets
from rest_framework.exceptions import ValidationError

from common import query
from common.ids import ID_REGEX
from common.periods import day_range
from common.permissions import IsAdminRole
from stats.periods import MIN_DATE, MSG_FUTURE, MSG_MIN_DATE, MSG_ORDER, parse_dates

from .models import AuditAction, AuditLog, AuditModule, AuditObjectType, AuditSource, AuditStatus
from .serializers import AuditLogSerializer

MSG_DATE_ORDER = MSG_ORDER

_FILTERS = [
    OpenApiParameter("module", str, enum=AuditModule.values, description="Bo‘lim"),
    OpenApiParameter("action", str, enum=AuditAction.values, description="Amal"),
    OpenApiParameter("actor", int, description="Foydalanuvchi ID"),
    OpenApiParameter("object_type", str, enum=AuditObjectType.values),
    OpenApiParameter("object_id", int),
    OpenApiParameter("source", str, enum=AuditSource.values),
    OpenApiParameter("date_from", OpenApiTypes.DATE, description="created_at mahalliy sanasi, shu kun ham kiradi"),
    OpenApiParameter("date_to", OpenApiTypes.DATE, description="created_at mahalliy sanasi, shu kun ham kiradi"),
    OpenApiParameter("search", str, description="actor_name, object_repr, op_id (qism), ip (aniq)"),
    OpenApiParameter("household", int, description="Shu xonadonga tegishli yozuvlar (TZ §14)"),
    OpenApiParameter("product", int, description="Shu mahsulotga tegishli yozuvlar"),
    OpenApiParameter("status", str, enum=AuditStatus.values, description="SUCCESS | FAILED"),
]


@extend_schema_view(list=extend_schema(parameters=_FILTERS), retrieve=extend_schema())
class AuditLogViewSet(mixins.ListModelMixin, mixins.RetrieveModelMixin, viewsets.GenericViewSet):
    """Read-only audit journal, ADMIN only. Every write method answers ``405``."""

    permission_classes = [IsAdminRole]
    serializer_class = AuditLogSerializer
    lookup_value_regex = ID_REGEX

    def get_queryset(self) -> QuerySet[AuditLog]:
        qs = AuditLog.objects.select_related("actor").order_by("-created_at", "-id")
        if self.action != "list":
            return qs
        request = self.request
        for name, choices in (
            ("module", AuditModule),
            ("action", AuditAction),
            ("source", AuditSource),
            ("object_type", AuditObjectType),
            ("status", AuditStatus),
        ):
            value = query.get_choice(request, name, choices)
            if value:
                qs = qs.filter(**{name: value})
        actor = query.get_int(request, "actor")
        if actor is not None:
            qs = qs.filter(actor_id=actor)
        object_id = query.get_int(request, "object_id")
        if object_id is not None:
            qs = qs.filter(object_id=object_id)
        household = query.get_int(request, "household")
        if household is not None:
            qs = qs.filter(household_ref=household)
        product = query.get_int(request, "product")
        if product is not None:
            qs = qs.filter(product_ref=product)
        date_from, date_to = self._dates()
        start, end = day_range(date_from, date_to)
        if start is not None:
            qs = qs.filter(created_at__gte=start)
        if end is not None:
            qs = qs.filter(created_at__lt=end)
        search = query.get_str(request, "search")
        if search:
            condition = Q(actor_name__icontains=search) | Q(object_repr__icontains=search) | Q(op_id__icontains=search)
            try:
                condition |= Q(ip=str(ipaddress.ip_address(search)))
            except ValueError:
                pass
            qs = qs.filter(condition)
        return qs

    def _dates(self):
        """``date_from`` / ``date_to`` checked as API_CONTRACT_V2 §8.1 without the 366-day limit.

        Besides matching the contract, the bounds keep ``day_range`` away from ``date.min`` / ``date.max``
        (``0001-01-01`` or ``9999-12-31`` used to raise ``OverflowError`` → 500).
        """
        values = parse_dates(self.request.query_params)
        date_from, date_to = values["date_from"], values["date_to"]
        today = timezone.localdate()
        errors: dict[str, list[str]] = {}
        if date_to is not None and date_to > today:
            errors["date_to"] = [MSG_FUTURE]
        if date_from is not None and date_to is not None and date_from > date_to:
            errors["date_from"] = [MSG_DATE_ORDER]
        elif date_from is not None and date_from < MIN_DATE:
            errors["date_from"] = [MSG_MIN_DATE]
        if errors:
            raise ValidationError(errors)
        return date_from, date_to

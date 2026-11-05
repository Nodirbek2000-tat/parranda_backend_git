"""Parsing of list filter query parameters with Uzbek error messages."""

from __future__ import annotations

from datetime import date

from django.db.models import QuerySet
from django.utils.dateparse import parse_date
from rest_framework.exceptions import ValidationError
from rest_framework.request import Request

from .ids import fits_db_int

_TRUE = {"1", "true", "yes"}
_FALSE = {"0", "false", "no"}


def get_str(request: Request, name: str) -> str:
    return (request.query_params.get(name) or "").strip()


def get_bool(request: Request, name: str) -> bool | None:
    raw = get_str(request, name).lower()
    if not raw:
        return None
    if raw in _TRUE:
        return True
    if raw in _FALSE:
        return False
    raise ValidationError({name: ["Qiymat true yoki false bo‘lishi kerak."]})


def get_int(request: Request, name: str) -> int | None:
    raw = get_str(request, name)
    if not raw:
        return None
    try:
        value = int(raw)
    except ValueError:
        raise ValidationError({name: ["Butun son kiriting."]}) from None
    if not fits_db_int(value):
        raise ValidationError({name: ["Qiymat juda katta."]})
    return value


def get_date(request: Request, name: str) -> date | None:
    raw = get_str(request, name)
    if not raw:
        return None
    try:
        parsed = parse_date(raw)
    except ValueError:
        parsed = None
    if parsed is None:
        raise ValidationError({name: ["Sana noto‘g‘ri formatda (YYYY-MM-DD)."]})
    return parsed


def get_choice(request: Request, name: str, choices: type) -> str | None:
    raw = get_str(request, name)
    if not raw:
        return None
    if raw not in choices.values:
        raise ValidationError({name: [f"“{raw}” ruxsat etilgan qiymat emas."]})
    return raw


def apply_date_range(qs: QuerySet, request: Request, field: str) -> QuerySet:
    """Filter ``qs`` by the ``date_from`` / ``date_to`` query parameters (inclusive)."""
    date_from = get_date(request, "date_from")
    date_to = get_date(request, "date_to")
    if date_from:
        qs = qs.filter(**{f"{field}__gte": date_from})
    if date_to:
        qs = qs.filter(**{f"{field}__lte": date_to})
    return qs

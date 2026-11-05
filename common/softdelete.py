"""Soft delete of catalogue objects (households, products, vehicles, drivers).

A deleted row is kept so that history (movements, stock operations, orders, logistics tasks, acts, audit) keeps
pointing at it, but it disappears from the application:

- ``Model.objects`` (the DEFAULT manager) hides rows with ``deleted_at`` set → lists, details (404), search,
  pickers, serializer ``queryset=`` lookups, dashboards and statistics skip them automatically;
- ``Model.all_objects`` sees every row (integrity checks, internal stock locking, demo reset);
- Django's base manager stays a plain ``models.Manager`` (``Meta.base_manager_name`` is never set), so foreign key
  traversal from history rows (``movement.household``, ``tx.product``, ``task.vehicle``…) and
  ``refresh_from_db()`` still load deleted rows.
"""

from __future__ import annotations

from django.db import models
from rest_framework import status
from rest_framework.exceptions import APIException


class SoftDeleteManager(models.Manager):
    """Default manager without soft-deleted rows."""

    def get_queryset(self) -> models.QuerySet:
        return super().get_queryset().filter(deleted_at__isnull=True)


def deleted_label(value: str, pk: int, max_length: int) -> str:
    """``"<value> (o‘chirilgan #<pk>)"`` cut to ``max_length`` — the suffix (which makes it unique) always stays."""
    suffix = f" (o‘chirilgan #{pk})"
    return f"{value[: max(max_length - len(suffix), 0)]}{suffix}"[:max_length]


class InUse(APIException):
    """``409 {"detail": "..."}`` — the object is used by running work and cannot be deleted now."""

    status_code = status.HTTP_409_CONFLICT
    default_detail = "Obyekt hozir ishlatilmoqda, uni o‘chirib bo‘lmaydi."
    default_code = "in_use"

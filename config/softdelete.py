"""Soft delete: ``objects`` hides deleted rows, ``all_objects`` sees everything.

Django's base manager stays unfiltered, so history rows (stock moves, events…) still load a deleted
case/product/package through their foreign keys.
"""

from django.db import models


class AliveManager(models.Manager):
    def get_queryset(self):
        return super().get_queryset().filter(deleted_at__isnull=True)


from rest_framework.exceptions import APIException  # noqa: E402


class Conflict(APIException):
    """409 with an Uzbek ``detail`` (e.g. "still used by a package")."""

    status_code = 409
    default_detail = "Amalni bajarib bo‘lmaydi: ma’lumot boshqa joyda ishlatilmoqda."
    default_code = "conflict"

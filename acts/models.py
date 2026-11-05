"""Dalolatnomalar (API_CONTRACT_V4 §6): frozen snapshots of completed work, numbered ``DL-000001``."""

from __future__ import annotations

from django.conf import settings
from django.db import models
from django.db.models import Q
from django.utils import timezone

from common.roles import Role


class ActKind(models.TextChoices):
    ROLE = "role", "Rol dalolatnomasi"
    GENERAL = "general", "Umumiy dalolatnoma"
    TASK = "task", "Vazifa dalolatnomasi"


class Act(models.Model):
    """One generated act. ``snapshot`` is never changed after creation (the PDF is built from it)."""

    kind = models.CharField("Turi", max_length=16, choices=ActKind.choices, db_index=True)
    role = models.CharField("Rol", max_length=16, choices=Role.choices, blank=True, default="")
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="+",
        null=True,
        blank=True,
        verbose_name="Xodim",
    )
    household = models.ForeignKey(
        "farm.Household", on_delete=models.PROTECT, related_name="acts", null=True, blank=True, verbose_name="Xonadon"
    )
    task = models.ForeignKey(
        "builder.TaskInstance", on_delete=models.PROTECT, related_name="acts", null=True, blank=True,
        verbose_name="Vazifa",
    )
    date_from = models.DateField("Sanadan", null=True, blank=True)
    date_to = models.DateField("Sanagacha", null=True, blank=True)
    template = models.ForeignKey(
        "builder.Blueprint", on_delete=models.PROTECT, related_name="acts", null=True, blank=True,
        verbose_name="Hujjat shabloni",
    )
    template_version = models.PositiveIntegerField("Shablon versiyasi", null=True, blank=True)
    snapshot = models.JSONField("Mazmuni", default=dict)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+", verbose_name="Yaratgan"
    )
    created_at = models.DateTimeField("Yaratilgan", default=timezone.now, editable=False)

    class Meta:
        verbose_name = "Dalolatnoma"
        verbose_name_plural = "Dalolatnomalar"
        ordering = ("-created_at", "-id")
        indexes = [
            models.Index(fields=["kind", "role"], name="act_kind_role_idx"),
            models.Index(fields=["household", "created_at"], name="act_household_idx"),
        ]
        constraints = [
            models.CheckConstraint(condition=~Q(kind=ActKind.ROLE) | ~Q(role=""), name="act_role_kind_has_role"),
            models.CheckConstraint(
                condition=~Q(kind=ActKind.TASK) | Q(task__isnull=False), name="act_task_kind_has_task"
            ),
        ]

    @property
    def code(self) -> str:
        return f"DL-{self.pk:06d}" if self.pk else ""

    def __str__(self) -> str:
        return self.code

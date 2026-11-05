from __future__ import annotations

from django.contrib.auth.models import AbstractUser
from django.db import models

from common.roles import Role


class User(AbstractUser):
    """System user. A superuser is always treated as ``ADMIN`` (see ``effective_role``)."""

    role = models.CharField("Rol", max_length=16, choices=Role.choices, default=Role.OWNER)
    phone = models.CharField("Telefon", max_length=32, blank=True)
    telegram_id = models.BigIntegerField("Telegram ID", unique=True, null=True, blank=True)
    must_change_password = models.BooleanField("Parolni almashtirishi shart", default=False)
    # Incremented to revoke every JWT issued so far (see accounts/tokens.py).
    token_version = models.PositiveIntegerField("Sessiya versiyasi", default=0, editable=False)
    # Set when every session is ended (admin "end sessions", logout with all=true): a Telegram
    # initData signed before this moment cannot log in again (accounts/views.py TelegramAuthView).
    sessions_revoked_at = models.DateTimeField("Sessiyalar yakunlangan vaqt", null=True, blank=True, editable=False)

    class Meta(AbstractUser.Meta):
        verbose_name = "Foydalanuvchi"
        verbose_name_plural = "Foydalanuvchilar"
        indexes = [models.Index(fields=["role"])]

    @property
    def effective_role(self) -> str:
        return Role.ADMIN if self.is_superuser else self.role

    @property
    def full_name(self) -> str:
        return self.get_full_name().strip() or self.username

    @property
    def telegram_linked(self) -> bool:
        return self.telegram_id is not None

    @property
    def household_id_or_none(self) -> int | None:
        """Primary key of the household this OWNER runs (``None`` for staff)."""
        if self.effective_role != Role.OWNER:
            return None
        household = getattr(self, "household", None)
        return household.pk if household is not None else None

    def __str__(self) -> str:
        return f"{self.full_name} ({self.username})"

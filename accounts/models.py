import secrets

from django.contrib.auth.models import AbstractUser, UserManager
from django.db import models


class Role(models.TextChoices):
    SUPERADMIN = "SUPERADMIN", "Super Admin"
    ADMIN = "ADMIN", "Administrator"
    INSPECTOR = "INSPECTOR", "Inspektor"
    HR = "HR", "HR"
    DIRECTOR = "DIRECTOR", "Direktor"
    BUILDER = "BUILDER", "Qurilish"
    WAREHOUSE = "WAREHOUSE", "Omborchi"
    INCUBATOR = "INCUBATOR", "Inkubatsiya"
    LOGISTICS = "LOGISTICS", "Logistika"
    VET = "VET", "Veterinar"
    OWNER = "OWNER", "Xonadon egasi"
    ACCOUNTANT = "ACCOUNTANT", "Buxgalter"


class ActiveUserManager(UserManager):
    """Users that are not soft-deleted (blocked ones are included)."""

    def alive(self):
        return self.filter(deleted_at__isnull=True)

    def active_with_role(self, *roles):
        return self.filter(deleted_at__isnull=True, is_active=True, role__in=roles)


class User(AbstractUser):
    role = models.CharField("Rol", max_length=20, choices=Role.choices, default=Role.INSPECTOR)
    full_name = models.CharField("F.I.Sh.", max_length=200, blank=True)
    phone = models.CharField("Telefon", max_length=30, blank=True)
    telegram_id = models.BigIntegerField("Telegram ID", unique=True, null=True, blank=True)
    must_change_password = models.BooleanField("Parolni almashtirish shart", default=False)
    token_version = models.PositiveIntegerField(default=0)
    deleted_at = models.DateTimeField("O‘chirilgan", null=True, blank=True)

    objects = ActiveUserManager()

    class Meta:
        verbose_name = "Foydalanuvchi"
        verbose_name_plural = "Foydalanuvchilar"

    def save(self, *args, **kwargs):
        # Superuser = SUPERADMIN (both directions), so `createsuperuser` gives a working account.
        if self.is_superuser:
            self.role = Role.SUPERADMIN
        if self.role == Role.SUPERADMIN:
            self.is_superuser = self.is_staff = True
        if not self.full_name:
            self.full_name = f"{self.first_name} {self.last_name}".strip() or self.username
        super().save(*args, **kwargs)

    @property
    def is_superadmin(self) -> bool:
        return self.role == Role.SUPERADMIN

    @property
    def is_admin(self) -> bool:
        """ADMIN or SUPERADMIN."""
        return self.role in (Role.ADMIN, Role.SUPERADMIN)

    def __str__(self) -> str:
        return f"{self.full_name} ({self.username})"


_ALPHABET = "abcdefghjkmnpqrstuvwxyzABCDEFGHJKLMNPQRSTUVWXYZ23456789"


def make_temp_password(length: int = 10) -> str:
    return "".join(secrets.choice(_ALPHABET) for _ in range(length))

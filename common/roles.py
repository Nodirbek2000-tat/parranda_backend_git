"""Role helpers shared by every app (a Django superuser always acts as ADMIN = "Super Admin")."""

from __future__ import annotations

from typing import TYPE_CHECKING

from django.db import models

if TYPE_CHECKING:
    from accounts.models import User


class Role(models.TextChoices):
    ADMIN = "ADMIN", "Administrator"
    WAREHOUSE = "WAREHOUSE", "Omborchi"
    LOGISTICS = "LOGISTICS", "Logistika xodimi"
    VET = "VET", "Veterinar"
    OWNER = "OWNER", "Xonadon egasi"


#: ADMIN + WAREHOUSE (Phase 1 "staff": dashboards, stats, warehouse).
STAFF_ROLES = frozenset({Role.ADMIN, Role.WAREHOUSE})
#: Every role an account can have (API_CONTRACT_V3 §1.1).
ALL_ROLES = frozenset(Role.values)
#: Roles an admin may create through ``POST /api/users`` (OWNER comes with a household).
CREATABLE_STAFF_ROLES = (Role.ADMIN, Role.WAREHOUSE, Role.LOGISTICS, Role.VET)
#: Roles a notification setting may target (OWNER is only ever a contextual recipient).
NOTIFIABLE_ROLES = (Role.ADMIN, Role.WAREHOUSE, Role.LOGISTICS, Role.VET)
#: ``(value, label)`` choices of the 4 staff roles (OpenAPI ``StaffRoleEnum``).
STAFF_ROLE_CHOICES = [(role.value, role.label) for role in CREATABLE_STAFF_ROLES]

SUPER_ADMIN_DISPLAY = "Super Admin"


def role_of(user: User | None) -> str | None:
    """Effective role of an authenticated user, or ``None`` for anonymous users."""
    if user is None or not user.is_authenticated:
        return None
    if user.is_superuser:
        return Role.ADMIN
    return user.role


def is_super_admin(user: User | None) -> bool:
    """Super Admin = Django superuser (API_CONTRACT_V3 §1.1). Effective role stays ADMIN."""
    return bool(user is not None and user.is_authenticated and user.is_active and user.is_superuser)


def role_display(user: User) -> str:
    """``Super Admin`` for superusers, otherwise the label of the stored role."""
    if user.is_superuser:
        return SUPER_ADMIN_DISPLAY
    return Role(user.role).label if user.role in ALL_ROLES else user.role

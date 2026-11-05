"""Role based DRF permission classes (API_CONTRACT_V3 §1.1–1.2).

Use the ready classes below or build one with :func:`IsRoleIn` / :func:`ReadOnlyFor`::

    permission_classes = [IsRoleIn(Role.ADMIN, Role.LOGISTICS)]
    permission_classes = [IsRoleIn(Role.ADMIN, Role.LOGISTICS) | ReadOnlyFor(Role.WAREHOUSE)]

Rules:
- A role without access to a whole endpoint group gets ``403``.
- “read” access (GET/HEAD/OPTIONS only) answers write methods with ``403`` and the Phase 1 text.
- OWNER isolation is NOT a permission: querysets filter by household and other objects answer ``404``.
"""

from __future__ import annotations

from rest_framework.permissions import SAFE_METHODS, BasePermission

from .roles import ALL_ROLES, STAFF_ROLES, Role, is_super_admin, role_of

MSG_NO_PERMISSION = "Sizda bu amal uchun ruxsat yo‘q."
MSG_SUPER_ADMIN_ONLY = "Bu amal faqat Super Admin uchun."


class _RolePermission(BasePermission):
    roles: frozenset[str] = frozenset()
    safe_only = False

    def has_permission(self, request, view) -> bool:
        if self.safe_only and request.method not in SAFE_METHODS:
            return False
        return role_of(request.user) in self.roles


def IsRoleIn(*roles: str) -> type[BasePermission]:  # noqa: N802 - reads like a class
    """Permission class admitting the given effective roles (superuser counts as ADMIN)."""
    allowed = frozenset(roles)
    unknown = allowed - ALL_ROLES
    if unknown:
        raise ValueError(f"Unknown roles: {sorted(unknown)}")
    name = "IsRoleIn_" + "_".join(sorted(allowed))
    return type(name, (_RolePermission,), {"roles": allowed, "message": MSG_NO_PERMISSION})


def ReadOnlyFor(*roles: str) -> type[BasePermission]:  # noqa: N802
    """Admits the roles for safe methods only (combine with ``|``: write → 403 for them)."""
    allowed = frozenset(roles)
    name = "ReadOnlyFor_" + "_".join(sorted(allowed))
    return type(name, (_RolePermission,), {"roles": allowed, "safe_only": True, "message": MSG_NO_PERMISSION})


class IsAdminRole(BasePermission):
    """ADMIN (or superuser) only."""

    def has_permission(self, request, view) -> bool:
        return role_of(request.user) == Role.ADMIN


class IsSuperAdmin(BasePermission):
    """Django superuser only (company settings, notification settings)."""

    message = MSG_SUPER_ADMIN_ONLY

    def has_permission(self, request, view) -> bool:
        return is_super_admin(request.user)


class IsStaffRole(BasePermission):
    """ADMIN or WAREHOUSE."""

    def has_permission(self, request, view) -> bool:
        return role_of(request.user) in STAFF_ROLES


class IsOwnerRole(BasePermission):
    """Household OWNER only."""

    def has_permission(self, request, view) -> bool:
        return role_of(request.user) == Role.OWNER


class IsAnyRole(BasePermission):
    """Phase 1 meaning kept on purpose: ADMIN, WAREHOUSE or OWNER (NOT LOGISTICS / VET).

    Phase 1 querysets scope only OWNER and return everything for other roles, so admitting L/V
    here would silently leak data. VET read access is granted endpoint by endpoint (V3 §1.2).
    """

    def has_permission(self, request, view) -> bool:
        return role_of(request.user) in {Role.ADMIN, Role.WAREHOUSE, Role.OWNER}


class IsAuthenticatedRole(BasePermission):
    """Any of the 5 roles (auth/me, change-password, logout, my notifications, settings GET, files ...)."""

    def has_permission(self, request, view) -> bool:
        return role_of(request.user) in ALL_ROLES


class IsAdminOrOwnerRole(BasePermission):
    """ADMIN or OWNER (used for daily reports)."""

    def has_permission(self, request, view) -> bool:
        return role_of(request.user) in {Role.ADMIN, Role.OWNER}


# Ready-made combinations used by the V3 modules.
IsLogistics = IsRoleIn(Role.LOGISTICS)
IsVet = IsRoleIn(Role.VET)
IsAdminOrLogistics = IsRoleIn(Role.ADMIN, Role.LOGISTICS)
IsAdminOrVet = IsRoleIn(Role.ADMIN, Role.VET)
IsAdminWarehouseOrLogistics = IsRoleIn(Role.ADMIN, Role.WAREHOUSE, Role.LOGISTICS)
IsAdminWarehouseOrOwner = IsRoleIn(Role.ADMIN, Role.WAREHOUSE, Role.OWNER)

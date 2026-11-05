"""Role references and territorial scope (API_CONTRACT_V4 §0, §2). Shared by builder, acts and farm."""

from __future__ import annotations

from typing import TYPE_CHECKING

from django.db.models import Q, QuerySet

from common.roles import ALL_ROLES, Role, role_of

if TYPE_CHECKING:
    from accounts.models import User

PROFILE_PREFIX = "profile:"


def access_of(user: User | None):
    """The user's :class:`builder.models.UserAccess` row or ``None`` (cached on the instance)."""
    if user is None or not getattr(user, "is_authenticated", False):
        return None
    from audit.context import current

    from .models import UserAccess

    # The cache lives for one HTTP request only (the per-request audit context is the key): a user instance
    # reused across requests (tests, long-lived objects) never sees a stale profile or scope.
    request_key = current()
    cached = getattr(user, "_nnp_access_cache", None)
    if request_key is not None and cached is not None and cached[0] is request_key:
        return cached[1]
    row = UserAccess.objects.select_related("profile").filter(user_id=user.pk).first()
    if request_key is not None:
        user._nnp_access_cache = (request_key, row)
    return row


def profile_id_of(ref: str) -> int | None:
    if ref.startswith(PROFILE_PREFIX):
        tail = ref[len(PROFILE_PREFIX):]
        return int(tail) if tail.isdigit() else None
    return None


def is_valid_ref(ref: str) -> bool:
    return ref in ALL_ROLES or profile_id_of(ref) is not None


def matches_ref(user: User, ref: str) -> bool:
    """Does ``user`` match the role reference ``"VET"`` / ``"profile:7"``?"""
    if not ref:
        return False
    pid = profile_id_of(ref)
    if pid is not None:
        access = access_of(user)
        return bool(access is not None and access.profile_id == pid)
    return role_of(user) == ref


def matches_any(user: User, refs) -> bool:
    return any(matches_ref(user, ref) for ref in refs or ())


def users_for_ref_q(ref: str) -> Q:
    """``Q`` over ``User`` for active, non-deleted users matching ``ref`` (superusers count as ADMIN)."""
    base = Q(is_active=True, deleted_at__isnull=True)
    pid = profile_id_of(ref)
    if pid is not None:
        return base & Q(access__profile_id=pid)
    if ref == Role.ADMIN:
        return base & (Q(role=Role.ADMIN) | Q(is_superuser=True))
    return base & Q(role=ref, is_superuser=False)


def has_scope(user: User | None) -> bool:
    if user is None or getattr(user, "is_superuser", False):
        return False
    access = access_of(user)
    if access is None:
        return False
    return bool(access.districts) or access.households.exists()


def scope_households(user: User | None, queryset: QuerySet, field: str = "") -> QuerySet:
    """Limit ``queryset`` to the households in the user's scope; unchanged without limits.

    ``field`` is the lookup path from the queryset's model to ``farm.Household`` (``""`` when the queryset
    *is* households, ``"household"`` for tasks …). Rows without a household stay visible.
    """
    if not has_scope(user):
        return queryset
    access = access_of(user)
    prefix = f"{field}__" if field else ""
    condition = Q()
    if access.districts:
        condition |= Q(**{f"{prefix}district__in": list(access.districts)})
    household_ids = list(access.households.values_list("pk", flat=True))
    if household_ids:
        condition |= Q(**{f"{prefix}pk__in": household_ids})
    if field:
        condition |= Q(**{f"{field}__isnull": True})
    return queryset.filter(condition)


def access_payload(user: User | None) -> dict:
    """``access`` block of ``GET /api/auth/me`` (§2). ``permissions: None`` = system role defaults."""
    access = access_of(user)
    if access is None or getattr(user, "is_superuser", False):
        return {"profile_id": None, "profile_name": None, "permissions": None, "districts": [], "household_ids": []}
    profile = access.profile
    return {
        "profile_id": access.profile_id,
        "profile_name": profile.name if profile is not None else None,
        "permissions": (profile.data or {}).get("permissions", {}) if profile is not None else None,
        "districts": list(access.districts or []),
        "household_ids": sorted(access.households.values_list("pk", flat=True)),
    }

"""Global search (API_CONTRACT_V3 §12) with a registry, so every module adds its own group.

A module registers its group in ``AppConfig.ready()``::

    from core import search
    from common.roles import Role

    def _orders(user, q, limit):
        qs = Order.objects.filter(...)            # scoped exactly like the list endpoint (OWNER → own)
        return [search.item(o.pk, o.code, o.household.name, f"/orders/{o.pk}") for o in qs[:limit]]

    search.register_group("order", "Buyurtmalar", {Role.ADMIN, Role.WAREHOUSE, Role.OWNER}, _orders, order=40)

Each group function runs ONE query (budget for the whole endpoint: ≤ 8) and returns at most ``limit`` items.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from django.db.models import Q

from accounts.models import User
from common.roles import Role, role_of

MAX_PER_GROUP = 5
MIN_Q, MAX_Q = 2, 100

Item = dict[str, Any]
GroupFn = Callable[[User, str, int], list[Item]]


@dataclass(frozen=True)
class Group:
    type: str
    label: str
    roles: frozenset[str]
    fn: GroupFn
    order: int


_GROUPS: dict[str, Group] = {}


def register_group(type_: str, label: str, roles: set[str] | frozenset[str], fn: GroupFn, *, order: int = 100) -> None:
    _GROUPS[type_] = Group(type_, label, frozenset(roles), fn, order)


def item(pk: int, title: str, subtitle: str, path: str) -> Item:
    return {"id": pk, "title": title, "subtitle": subtitle, "path": path}


def code_pk(q: str, prefix: str) -> int | None:
    """``XN-0003`` / ``xn-3`` → 3 for exact code matches (``None`` otherwise)."""
    match = re.fullmatch(rf"{re.escape(prefix)}-?0*(\d{{1,12}})", q.strip(), flags=re.IGNORECASE)
    return int(match.group(1)) if match else None


def search(user: User, q: str) -> dict[str, Any]:
    role = role_of(user)
    groups = []
    for group in sorted(_GROUPS.values(), key=lambda g: (g.order, g.type)):
        if role not in group.roles:
            continue
        items = group.fn(user, q, MAX_PER_GROUP)[:MAX_PER_GROUP]
        if items:
            groups.append({"type": group.type, "label": group.label, "items": items})
    return {"q": q, "groups": groups}


# --- Built-in groups (Phase 1 objects) --------------------------------------------------------


def _households(user: User, q: str, limit: int) -> list[Item]:
    from farm.models import Household

    qs = Household.objects.all()
    if role_of(user) == Role.OWNER:
        qs = qs.filter(owner=user)
    condition = Q(name__icontains=q) | Q(district__icontains=q) | Q(mfy__icontains=q) | Q(phone__icontains=q)
    pk = code_pk(q, "XN")
    if pk is not None:
        condition |= Q(pk=pk)
    owner = role_of(user) == Role.OWNER
    return [
        item(h.pk, h.name, f"{h.code} · {h.district}", "/my" if owner else f"/households/{h.pk}")
        for h in qs.filter(condition).order_by("id")[:limit]
    ]


def _batches(user: User, q: str, limit: int) -> list[Item]:
    from farm.models import PoultryBatch

    qs = PoultryBatch.objects.filter(Q(code__icontains=q) | Q(breed__icontains=q) | Q(supplier__icontains=q))
    return [
        item(b.pk, b.code, f"{b.get_poultry_type_display()} · {b.breed}", f"/batches/{b.pk}")
        for b in qs.order_by("-arrived_at", "-id")[:limit]
    ]


def _products(user: User, q: str, limit: int) -> list[Item]:
    from warehouse.models import Product

    return [
        item(p.pk, p.name, p.get_category_display(), f"/warehouse/{p.pk}")
        for p in Product.objects.filter(name__icontains=q).order_by("name", "id")[:limit]
    ]


def _users(user: User, q: str, limit: int) -> list[Item]:
    from common.roles import role_display

    qs = User.objects.filter(
        Q(username__icontains=q) | Q(first_name__icontains=q) | Q(last_name__icontains=q) | Q(phone__icontains=q),
        deleted_at__isnull=True,
    )
    if not user.is_superuser:
        qs = qs.exclude(is_superuser=True)
    return [item(u.pk, u.full_name, f"{u.username} · {role_display(u)}", "/users") for u in qs.order_by("id")[:limit]]


A, W, V, O = Role.ADMIN, Role.WAREHOUSE, Role.VET, Role.OWNER
register_group("household", "Xonadonlar", {A, W, V, O}, _households, order=10)
# Not for VET: /batches/{id} is an A/W page (the vet sees batch vaccinations under /vet and the household page).
register_group("batch", "Partiyalar", {A, W}, _batches, order=20)
register_group("product", "Mahsulotlar", {A, W}, _products, order=30)
# order (40), task (50), vet_visit (60) are registered by the orders / logistics / vet apps.
register_group("user", "Foydalanuvchilar", {A}, _users, order=90)

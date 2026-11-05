"""Global search group ``vet_visit`` (API_CONTRACT_V3 §12): A, V all; O own household (``/my/vet``)."""

from __future__ import annotations

from django.db.models import Q

from common.formatting import fmt_date
from common.roles import Role, role_of
from core import search

from .models import VetVisit


def _visits(user, q: str, limit: int) -> list[dict]:
    from .selectors import tables_ready

    if not tables_ready():
        return []
    owner = role_of(user) == Role.OWNER
    qs = VetVisit.objects.select_related("household")
    if owner:
        qs = qs.filter(household__owner=user)
    condition = Q(purpose__icontains=q) | Q(household__name__icontains=q) | Q(conclusion__icontains=q)
    pk = search.code_pk(q, "VT")
    if pk is not None:
        condition |= Q(pk=pk)
    rows = qs.filter(condition).order_by("-planned_date", "-id")[:limit]
    return [
        search.item(
            v.pk,
            f"{v.code} · {v.household.name}",
            f"{fmt_date(v.planned_date)} · {v.get_status_display()} · {v.purpose[:60]}",
            "/my/vet" if owner else f"/vet/visits/{v.pk}",
        )
        for v in rows
    ]


def register() -> None:
    search.register_group("vet_visit", "Veterinariya tekshiruvlari", {Role.ADMIN, Role.VET, Role.OWNER}, _visits, order=60)

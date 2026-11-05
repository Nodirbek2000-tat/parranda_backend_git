"""``check_integrity`` checks of the vet app (API_CONTRACT_V3 §16.3)."""

from __future__ import annotations

from django.db.models import F, Q

from .models import DiseaseRecord, DiseaseStatus, Treatment, Vaccination, VetStatus, VetVisit

LIMIT = 50


def problems() -> list[str]:
    """Invariants the services guarantee; every hit is a data error (fixed number of queries)."""
    found: list[str] = []
    missing_out = Q(product__isnull=False, quantity_used__gt=0, stock_transaction__isnull=True)
    for row in Vaccination.objects.filter(missing_out, status=VetStatus.DONE).order_by("id")[:LIMIT]:
        found.append(f"{row.code}: emlash o‘tkazilgan, vaksina sarflangan, lekin ombor chiqimi yo‘q")
    for row in Treatment.objects.filter(missing_out).order_by("id")[:LIMIT]:
        found.append(f"{row.code}: preparat sarflangan, lekin ombor chiqimi yo‘q")
    same_out = (
        Q(stock_transaction__product_id=F("product_id"))
        & Q(stock_transaction__quantity=F("quantity_used"))
        & Q(stock_transaction__household_id=F("household_id"))
        & Q(stock_transaction__type="OUT")
    )
    for model, label in ((Vaccination, "emlash"), (Treatment, "davolash")):
        for row in model.objects.filter(stock_transaction__isnull=False).exclude(same_out).order_by("id")[:LIMIT]:
            found.append(f"{row.code}: {label} va ombor chiqimi (mahsulot / miqdor / xonadon) mos emas")
    incomplete = Q(done_at__isnull=True) | Q(conclusion="") | Q(health_status="")
    for row in VetVisit.objects.filter(incomplete, status=VetStatus.DONE).order_by("id")[:LIMIT]:
        found.append(f"{row.code}: o‘tkazilgan tekshiruvda xulosa, holat yoki vaqt yo‘q")
    for row in DiseaseRecord.objects.filter(status=DiseaseStatus.RESOLVED, resolved_date__isnull=True).order_by("id")[:LIMIT]:
        found.append(f"{row.code}: tuzalgan kasallikda sana yo‘q")
    return found

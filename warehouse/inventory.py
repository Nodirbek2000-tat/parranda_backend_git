"""Inventory count (inventarizatsiya), API_CONTRACT_V3 §5.3.

Every state change: ``transaction.atomic()`` → lock the count (``select_for_update``) → re-validate the
CURRENT status (``409``) → lock products in ascending id order (lock order §17.1: InventoryCount → Product
→ StockLot) → write → audit in the same transaction → notification after commit.

Adjustments are posted only through :func:`warehouse.services.apply_adjustment` (ADJUST_IN / ADJUST_OUT,
lots consumed FEFO for ADJUST_OUT), so balances never change anywhere else.
"""

from __future__ import annotations

from collections.abc import Iterable
from decimal import Decimal

from django.db import transaction
from django.utils import timezone
from rest_framework import status
from rest_framework.exceptions import APIException, PermissionDenied, ValidationError

from accounts.models import User
from audit import services as audit
from audit.models import AuditAction, AuditModule
from common.formatting import fmt_number
from common.roles import Role, role_of

from . import events as warehouse_events
from . import services
from .models import (
    MAX_AMOUNT,
    WHOLE_UNITS,
    InventoryCount,
    InventoryCountLine,
    InventoryCountStatus,
    Product,
)

ZERO = Decimal("0")


class Conflict(APIException):
    """``409`` — the count is no longer in the status the action needs (double submit, stale page)."""

    status_code = status.HTTP_409_CONFLICT
    default_code = "conflict"

MSG_NO_PRODUCTS = "Inventarizatsiya uchun mahsulot topilmadi."
MSG_WRONG_STATUS = "Inventarizatsiya holati: {status}. Amalni bajarib bo‘lmaydi."
MSG_UNCOUNTED = "{count} ta mahsulot sanalmagan."
MSG_BELOW_RESERVED = "“{name}”: sanalgan miqdor zaxiralangan miqdordan ({reserved} {unit}) kam."
MSG_NOT_A_LINE = "Mahsulot bu inventarizatsiyada yo‘q: {ids}."
MSG_DUPLICATE_LINE = "Mahsulot bir necha marta ko‘rsatilgan: {ids}."
MSG_WHOLE = "“{name}”: “{unit}” uchun butun son kiriting."
MSG_TOO_BIG = "Miqdor {max} dan oshmasligi kerak."
MSG_NEGATIVE = "Miqdor manfiy bo‘lishi mumkin emas."


def _lock(count_id: int) -> InventoryCount:
    return InventoryCount.objects.select_for_update().get(pk=count_id)


def _require_draft(count: InventoryCount) -> None:
    if count.status != InventoryCountStatus.DRAFT:
        raise Conflict(MSG_WRONG_STATUS.format(status=count.get_status_display()))


def create_count(
    *, actor: User, note: str = "", category: str | None = None, product_ids: Iterable[int] | None = None
) -> InventoryCount:
    """DRAFT count with one line per active product (optionally one category / the listed ids).

    ``expected_qty`` is the balance snapshot at creation (read under the product locks so the snapshot is
    a consistent state); approval uses the live balance anyway.
    """
    with transaction.atomic():
        qs = Product.objects.filter(is_active=True)
        if category:
            qs = qs.filter(category=category)
        if product_ids is not None:
            qs = qs.filter(pk__in=list(product_ids))
        ids = list(qs.order_by("pk").values_list("pk", flat=True))
        if not ids:
            raise ValidationError({"non_field_errors": [MSG_NO_PRODUCTS]})
        count = InventoryCount.objects.create(note=(note or "").strip()[:500], created_by=actor)
        products = services.lock_products(ids)
        InventoryCountLine.objects.bulk_create(
            [InventoryCountLine(count=count, product_id=pk, expected_qty=products[pk].balance) for pk in ids]
        )
        audit.record_create(AuditModule.INVENTORY, count, actor, extra={"lines": len(ids), "category": category})
    return count


def update_lines(*, actor: User, count_id: int, lines: list[dict]) -> InventoryCount:
    """``{"product": id, "counted_qty": Decimal | None}`` per line; ``None`` clears. DRAFT only."""
    with transaction.atomic():
        count = _lock(count_id)
        _require_draft(count)
        wanted = [int(item["product"]) for item in lines]
        duplicates = sorted({pk for pk in wanted if wanted.count(pk) > 1})
        if duplicates:
            raise ValidationError({"lines": [MSG_DUPLICATE_LINE.format(ids=", ".join(map(str, duplicates)))]})
        existing = {
            line.product_id: line
            for line in InventoryCountLine.objects.select_related("product").filter(count=count, product_id__in=wanted)
        }
        missing = [pk for pk in wanted if pk not in existing]
        if missing:
            raise ValidationError({"lines": [MSG_NOT_A_LINE.format(ids=", ".join(map(str, missing)))]})
        errors: list[str] = []
        changed: list[InventoryCountLine] = []
        now = timezone.now()
        before: dict[int, str | None] = {}
        after: dict[int, str | None] = {}
        for item in lines:
            line = existing[int(item["product"])]
            value = item.get("counted_qty")
            if value is not None:
                product = line.product
                if value < 0:
                    errors.append(MSG_NEGATIVE)
                    continue
                if value > MAX_AMOUNT:
                    errors.append(MSG_TOO_BIG.format(max=fmt_number(MAX_AMOUNT)))
                    continue
                if product.unit in WHOLE_UNITS and value != value.to_integral_value():
                    errors.append(MSG_WHOLE.format(name=product.name, unit=product.unit))
                    continue
            if line.counted_qty == value:
                continue
            before[line.product_id] = None if line.counted_qty is None else str(line.counted_qty)
            after[line.product_id] = None if value is None else str(value)
            line.counted_qty = value
            line.counted_at = now if value is not None else None
            changed.append(line)
        if errors:
            raise ValidationError({"lines": errors})
        if changed:
            InventoryCountLine.objects.bulk_update(changed, ["counted_qty", "counted_at"])
            audit.record(
                module=AuditModule.INVENTORY,
                action=AuditAction.UPDATE,
                obj=count,
                actor=actor,
                before={"counted": {str(k): v for k, v in before.items()}},
                after={"counted": {str(k): v for k, v in after.items()}},
            )
    return count


def approve(*, actor: User, count_id: int) -> InventoryCount:
    """DRAFT → APPROVED: one ADJUST_IN / ADJUST_OUT per line with a difference against the LIVE balance."""
    with transaction.atomic():
        count = _lock(count_id)
        _require_draft(count)
        lines = list(InventoryCountLine.objects.filter(count=count).order_by("product_id"))
        uncounted = sum(1 for line in lines if line.counted_qty is None)
        if uncounted:
            raise ValidationError({"lines": [MSG_UNCOUNTED.format(count=uncounted)]})
        products = services.lock_products(line.product_id for line in lines)
        for line in lines:
            product = products[line.product_id]
            if line.counted_qty < product.reserved:
                raise ValidationError(
                    {
                        "lines": [
                            MSG_BELOW_RESERVED.format(
                                name=product.name, reserved=fmt_number(product.reserved), unit=product.unit
                            )
                        ]
                    }
                )
        now = timezone.now()
        surplus = shortage = 0
        for line in lines:
            product = products[line.product_id]
            line.balance_at_approval = product.balance
            line.difference = line.counted_qty - product.balance
            line.adjustment = services.apply_adjustment(
                actor=actor,
                product=product,
                difference=line.difference,
                document_no=count.code,
                note=f"{count.code} inventarizatsiya",
                count_id=count.pk,
            )
            if line.difference > 0:
                surplus += 1
            elif line.difference < 0:
                shortage += 1
        InventoryCountLine.objects.bulk_update(lines, ["balance_at_approval", "difference", "adjustment"])
        count.status = InventoryCountStatus.APPROVED
        count.approved_by = actor
        count.approved_at = now
        count.save(update_fields=["status", "approved_by", "approved_at"])
        audit.record(
            module=AuditModule.INVENTORY,
            action=AuditAction.APPROVE,
            obj=count,
            actor=actor,
            before={"status": InventoryCountStatus.DRAFT},
            after={
                "status": InventoryCountStatus.APPROVED,
                "lines": len(lines),
                "surplus": surplus,
                "shortage": shortage,
                "adjustments": [line.adjustment.op_id for line in lines if line.adjustment is not None],
            },
        )
        warehouse_events.inventory_approved(count, lines=len(lines), surplus=surplus, shortage=shortage)
    return count


def cancel(*, actor: User, count_id: int, reason: str) -> InventoryCount:
    with transaction.atomic():
        count = _lock(count_id)
        _require_draft(count)
        count.status = InventoryCountStatus.CANCELLED
        count.cancelled_by = actor
        count.cancelled_at = timezone.now()
        count.cancel_reason = reason.strip()[:500]
        count.save(update_fields=["status", "cancelled_by", "cancelled_at", "cancel_reason"])
        audit.record(
            module=AuditModule.INVENTORY,
            action=AuditAction.STATUS_CHANGE,
            obj=count,
            actor=actor,
            before={"status": InventoryCountStatus.DRAFT},
            after={"status": InventoryCountStatus.CANCELLED, "reason": count.cancel_reason},
        )
    return count


MSG_DELETE_APPROVED_ADMIN = "Tasdiqlangan inventarizatsiyani faqat administrator o‘chira oladi."
MSG_DELETE_HAS_ADJUSTMENTS = (
    "Inventarizatsiya bo‘yicha tuzatish operatsiyalari o‘tkazilgan ({ops}). Qoralama holatida uni o‘chirib bo‘lmaydi."
)


def delete_count(*, actor: User, count_id: int) -> None:
    """Soft delete (``DELETE /api/inventory-counts/{id}``): the count disappears from the list and detail (404).

    ``DRAFT`` / ``CANCELLED`` (A, W): nothing was posted, so nothing changes in stock. ``APPROVED`` (A only, the
    approver's role): only hidden — its ADJUST_IN / ADJUST_OUT operations stay posted and keep the balances and
    lots right. A ``DRAFT`` with posted adjustments (never produced by the services) is refused with ``409``.
    """
    with transaction.atomic():
        count = InventoryCount.all_objects.select_for_update(of=("self",)).get(pk=count_id)
        if count.deleted_at is not None:
            return
        if count.status == InventoryCountStatus.APPROVED and role_of(actor) != Role.ADMIN:
            raise PermissionDenied(MSG_DELETE_APPROVED_ADMIN)
        if count.status != InventoryCountStatus.APPROVED:
            posted = list(
                InventoryCountLine.objects.filter(count=count, adjustment__isnull=False)
                .order_by("id")
                .values_list("adjustment_id", flat=True)
            )
            if posted:
                ops = ", ".join(f"OM-{pk:06d}" for pk in posted[:3])
                raise Conflict(MSG_DELETE_HAS_ADJUSTMENTS.format(ops=ops))
        before = audit.snapshot(count)
        count.deleted_at = timezone.now()
        count.save(update_fields=["deleted_at"])
        audit.record(
            module=AuditModule.INVENTORY,
            action=AuditAction.DELETE,
            obj=count,
            actor=actor,
            before=before,
            after={"deleted": True, "status": count.status},
        )

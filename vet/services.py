"""Business logic of the vet app (API_CONTRACT_V3 §8).

Every state change: ``transaction.atomic()`` → ``select_for_update()`` (lock order §17.1: vet objects →
PoultryBatch → Product → Attachment) → validate the CURRENT status (``409`` otherwise) → change rows →
``audit.services.record*`` (same transaction) → ``vet.events.*`` (queued after commit only).

Stock: vaccine / drug usage is an ``OUT`` created through ``warehouse.services.record_stock_transaction`` (the only
place that changes balances and lots); the OUT is linked one-to-one, so it can never be created twice.
"""

from __future__ import annotations

import inspect
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Any

from django.db import transaction
from django.utils import timezone
from rest_framework import status
from rest_framework.exceptions import APIException, PermissionDenied, ValidationError

from accounts.models import User
from audit import services as audit
from audit.models import AuditAction, AuditModule
from common.roles import Role, role_of
from core import files
from core.models import AttachmentPurpose
from farm import services as farm_services
from farm.models import Household, PoultryBatch, PoultryMovement
from warehouse.models import Product, ProductCategory, StockTransaction, StockTransactionType

from . import events
from .models import (
    DiseaseRecord,
    DiseaseStatus,
    HealthStatus,
    RequestStatus,
    Treatment,
    Vaccination,
    VetRequest,
    VetStatus,
    VetVisit,
)

MSG_HOUSEHOLD_INACTIVE = "Xonadon faol emas"
MSG_BATCH_NOT_DISTRIBUTED = "Bu partiya ushbu xonadonga tarqatilmagan."
MSG_VET_ONLY_ACTIVE = "Faqat faol veterinarni tanlash mumkin."
MSG_DATE_RANGE = "Sana oralig‘i noto‘g‘ri."
MSG_OTHER_VET_VISIT = "Bu tekshiruv boshqa veterinarga biriktirilgan."
MSG_OTHER_VET_VACCINATION = "Bu emlash boshqa veterinarga biriktirilgan."
MSG_REASSIGN_ADMIN_ONLY = "Boshqa veterinarga faqat administrator biriktira oladi."
MSG_VACCINE_ONLY = "Faqat “Vaksina” toifasidagi mahsulot tanlanadi."
MSG_DRUG_ONLY = "Faqat “Veterinariya preparati” yoki “Vaksina” toifasidagi mahsulot tanlanadi."
MSG_PRODUCT_INACTIVE = "Mahsulot faol emas"
MSG_VACCINE_NAME = "Vaksina nomini kiriting yoki mahsulotni tanlang."
MSG_DRUG_NAME = "Preparat nomini kiriting yoki mahsulotni tanlang."
MSG_DISEASE_OTHER_HOUSEHOLD = "Kasallik yozuvi boshqa xonadonga tegishli."
MSG_DISEASE_RESOLVED = "Kasallik tuzalgan deb yopilgan."
MSG_VISIT_OTHER_HOUSEHOLD = "Tekshiruv boshqa xonadonga tegishli."
MSG_REQUEST_OTHER_HOUSEHOLD = "Murojaat boshqa xonadonga tegishli."
MSG_REQUEST_CLOSED = "Murojaat yopilgan."
MSG_OPEN_REQUESTS = "Sizda 5 ta ochiq murojaat bor. Avval javobni kuting."
MSG_FUTURE_DATE = "Sana kelajakda bo‘lishi mumkin emas"
MSG_FUTURE_TIME = "Vaqt kelajakda bo‘lishi mumkin emas"
MSG_CAMPAIGN_BATCH = "Ommaviy emlashda partiya tanlanmaydi."
MSG_RESOLVED_BEFORE = "Tuzalgan sana aniqlangan sanadan oldin bo‘lishi mumkin emas."
MSG_END_BEFORE_START = "Tugash sanasi boshlanish sanasidan oldin bo‘lishi mumkin emas."
OWNER_CLOSE_RESPONSE = "Egasi tomonidan bekor qilindi"
MSG_RESPONSE_REQUIRED = "Javobni kiriting (kamida 3 ta belgi)."

MAX_OPEN_REQUESTS = 5
VISIT_PAST_DAYS = 30
VISIT_FUTURE_DAYS = 365
VACCINATION_PAST_DAYS = 30
VACCINATION_FUTURE_DAYS = 365
DONE_DATE_SLACK_DAYS = 30
TREATMENT_FUTURE_DAYS = 30


class Conflict(APIException):
    """``409`` — the object is no longer in a status that allows the action (double submit, stale page)."""

    status_code = status.HTTP_409_CONFLICT
    default_detail = "Amalni bajarib bo‘lmaydi."
    default_code = "conflict"


def _conflict(obj: Any, label: str) -> Conflict:
    return Conflict(f"{label} holati: {obj.get_status_display()}. Amalni bajarib bo‘lmaydi.")


# --- Shared validation ---------------------------------------------------------------------------


def _today() -> date:
    return timezone.localdate()


def check_household_active(household: Household, field: str = "household") -> None:
    if not household.is_active:
        raise ValidationError({field: [MSG_HOUSEHOLD_INACTIVE]})


def check_batch(batch: PoultryBatch | None, household: Household) -> None:
    """The batch must have at least one movement with that household (§8)."""
    if batch is None:
        return
    if not PoultryMovement.objects.filter(batch_id=batch.pk, household_id=household.pk).exists():
        raise ValidationError({"batch": [MSG_BATCH_NOT_DISTRIBUTED]})


def check_vet(user: User | None) -> None:
    if user is None:
        return
    if not user.is_active or user.is_superuser or user.role != Role.VET:
        raise ValidationError({"vet": [MSG_VET_ONLY_ACTIVE]})


def resolve_vet(actor: User, data: dict[str, Any]) -> User | None:
    """Explicit ``vet``; a VET creating without one becomes the vet himself."""
    vet = data.get("vet")
    if vet is None and role_of(actor) == Role.VET:
        vet = actor
    check_vet(vet)
    return vet


def check_date_window(value: date, field: str, *, past: int, future: int) -> None:
    today = _today()
    if not today - timedelta(days=past) <= value <= today + timedelta(days=future):
        raise ValidationError({field: [MSG_DATE_RANGE]})


def check_product(product: Product | None, categories: set[str], message: str) -> None:
    if product is None:
        return
    if product.category not in categories:
        raise ValidationError({"product": [message]})
    if not product.is_active:
        raise ValidationError({"product": [MSG_PRODUCT_INACTIVE]})


def _pk(obj: Any) -> int | None:
    return obj.pk if obj is not None else None


def _assigned_check(obj: VetVisit | Vaccination, actor: User, message: str) -> None:
    """Only the assigned vet acts; an unassigned object is taken over by the acting vet (§8.1)."""
    if obj.vet_id is not None and obj.vet_id != actor.pk:
        raise PermissionDenied(message)
    if obj.vet_id is None:
        obj.vet = actor


def _vet_edit_check(obj: VetVisit | Vaccination, actor: User, changes: dict[str, Any], message: str) -> None:
    """A VET edits only his own (or an unassigned) row and may only assign it to himself.

    Reassigning to another vet or clearing the vet stays with ADMIN; otherwise a vet could take over a
    colleague's visit/vaccination with ``PATCH {"vet": me}`` and pass the complete-time check (§8.1).
    """
    if role_of(actor) != Role.VET:
        return
    if obj.vet_id is not None and obj.vet_id != actor.pk:
        raise PermissionDenied(message)
    if "vet" in changes:
        new_vet = _pk(changes["vet"])
        if new_vet != obj.vet_id and new_vet != actor.pk:
            raise PermissionDenied(MSG_REASSIGN_ADMIN_ONLY)


def _status_change(module_obj: Any, actor: User, before: dict[str, Any], after: dict[str, Any]) -> None:
    audit.record(
        module=AuditModule.VET,
        action=AuditAction.STATUS_CHANGE,
        obj=module_obj,
        actor=actor,
        before=before,
        after=after,
    )


def _stock_out(
    *,
    actor: User,
    product: Product,
    quantity: Decimal,
    household: Household,
    document_no: str,
    note: str,
    reference: tuple[str, int],
) -> StockTransaction:
    """OUT through the warehouse posting service (lots FEFO, ``available``, ``low_stock``, audit ``stock``).

    ``stock_out`` of these OUTs goes to WAREHOUSE only (§9.4, ``owner_notified=False``) and the row carries
    ``reference = (vaccination|treatment, id)`` (§5.4). Uses ``post_reference_out`` when the warehouse module
    provides it, otherwise ``post_locked`` on the product locked here (``quantity ≤ available`` checked first).
    Errors of the quantity map to ``quantity_used``.
    """
    from warehouse import services as ws

    note = note[:500]
    try:
        post = getattr(ws, "post_reference_out", None)
        if post is not None:
            values = {
                "actor": actor,
                "product": product,
                "product_id": product.pk,
                "quantity": quantity,
                "household": household,
                "document_no": document_no,
                "note": note,
                "reference": reference,
                "reference_type": reference[0],
                "reference_id": reference[1],
            }
            params = inspect.signature(post).parameters
            return post(**{name: value for name, value in values.items() if name in params})
        locked = ws.lock_products([product.pk])[product.pk]
        if not locked.is_active:
            raise ValidationError({"product": [MSG_PRODUCT_INACTIVE]})
        error = ws._whole_units_error(locked, quantity) or ws._out_error(locked, quantity)
        if error:
            raise ValidationError({"quantity_used": [error]})
        return ws.post_locked(
            actor=actor,
            product=locked,
            transaction_type=StockTransactionType.OUT,
            quantity=quantity,
            document_no=document_no,
            household=household,
            note=note,
            notify_owner=False,
            reference=reference,
        )
    except ValidationError as exc:
        detail = exc.detail
        if isinstance(detail, dict) and "quantity" in detail:
            raise ValidationError({"quantity_used": detail["quantity"]}) from None
        raise


# --- Visits (§8.1) --------------------------------------------------------------------------------


def _lock_visit(pk: int) -> VetVisit:
    return VetVisit.objects.select_for_update(of=("self",)).select_related("household__owner", "batch", "vet").get(pk=pk)


def _set_request_in_progress(request_obj: VetRequest, actor: User, *, vet: User | None) -> None:
    """NEW → IN_PROGRESS (locked by the caller). Audit + owner notification."""
    if request_obj.status != RequestStatus.NEW:
        return
    request_obj.status = RequestStatus.IN_PROGRESS
    if request_obj.vet_id is None and vet is not None:
        request_obj.vet = vet
    request_obj.save(update_fields=["status", "vet", "updated_at"])
    _status_change(
        request_obj,
        actor,
        {"status": RequestStatus.NEW},
        {"status": RequestStatus.IN_PROGRESS, "vet_id": request_obj.vet_id},
    )
    events.request_updated(request_obj)


def plan_visit(*, actor: User, data: dict[str, Any]) -> VetVisit:
    """``POST /api/vet/visits`` and ``POST /api/vet/requests/{id}/plan-visit``."""
    with transaction.atomic():
        request_obj: VetRequest | None = data.get("request")
        if request_obj is not None:
            request_obj = VetRequest.objects.select_for_update(of=("self",)).select_related("household__owner").get(
                pk=request_obj.pk
            )
            if request_obj.status == RequestStatus.CLOSED:
                raise ValidationError({"request": [MSG_REQUEST_CLOSED]})
            if data.get("household") is None:
                data["household"] = request_obj.household
            elif data["household"].pk != request_obj.household_id:
                raise ValidationError({"request": [MSG_REQUEST_OTHER_HOUSEHOLD]})
        household: Household = data["household"]
        check_household_active(household)
        check_date_window(data["planned_date"], "planned_date", past=VISIT_PAST_DAYS, future=VISIT_FUTURE_DAYS)
        batch = data.get("batch")
        check_batch(batch, household)
        vet = resolve_vet(actor, data)
        visit = VetVisit.objects.create(
            household=household,
            batch=batch,
            vet=vet,
            planned_date=data["planned_date"],
            purpose=data["purpose"],
            request=request_obj,
            created_by=actor,
        )
        audit.record_create(AuditModule.VET, visit, actor)
        events.visit_planned(visit)
        if request_obj is not None:
            _set_request_in_progress(request_obj, actor, vet=actor if role_of(actor) == Role.VET else vet)
    return visit


VISIT_EDITABLE = ("planned_date", "purpose", "batch", "vet")


def update_visit(*, actor: User, pk: int, changes: dict[str, Any]) -> VetVisit:
    """``PATCH``: PLANNED only; a changed date or vet notifies again (new key)."""
    with transaction.atomic():
        visit = _lock_visit(pk)
        if visit.status != VetStatus.PLANNED:
            raise _conflict(visit, "Tekshiruv")
        _vet_edit_check(visit, actor, changes, MSG_OTHER_VET_VISIT)
        if "planned_date" in changes:
            check_date_window(changes["planned_date"], "planned_date", past=VISIT_PAST_DAYS, future=VISIT_FUTURE_DAYS)
        if "batch" in changes:
            check_batch(changes["batch"], visit.household)
        if "vet" in changes:
            check_vet(changes["vet"])
        before = audit.snapshot(visit)
        old_key = events.visit_key(visit)
        for name in VISIT_EDITABLE:
            if name in changes:
                setattr(visit, name, changes[name])
        visit.save()
        audit.record_update(AuditModule.VET, visit, actor, before)
        if events.visit_key(visit) != old_key:
            events.visit_planned(visit)
    return visit


def complete_visit(*, actor: User, pk: int, data: dict[str, Any]) -> VetVisit:
    """PLANNED → DONE with the conclusion (VET only; the assigned one, or any vet for an unassigned visit)."""
    now = timezone.now()
    done_at: datetime = data.get("done_at") or now
    if done_at > now:
        raise ValidationError({"done_at": [MSG_FUTURE_TIME]})
    with transaction.atomic():
        visit = _lock_visit(pk)
        if visit.status != VetStatus.PLANNED:
            raise _conflict(visit, "Tekshiruv")
        _assigned_check(visit, actor, MSG_OTHER_VET_VISIT)
        visit.status = VetStatus.DONE
        visit.done_at = done_at
        visit.health_status = data["health_status"]
        visit.birds_checked = data.get("birds_checked")
        visit.findings = data.get("findings", "")
        visit.conclusion = data["conclusion"]
        visit.recommendations = data.get("recommendations", "")
        visit.save()
        _status_change(
            visit,
            actor,
            {"status": VetStatus.PLANNED},
            {
                "status": VetStatus.DONE,
                "vet_id": visit.vet_id,
                "done_at": visit.done_at,
                "health_status": visit.health_status,
                "birds_checked": visit.birds_checked,
                "conclusion": visit.conclusion,
            },
        )
        files.bind_files(data.get("file_ids") or [], actor=actor, purpose=AttachmentPurpose.VET_VISIT, obj=visit)
        if data.get("update_batch_health") and visit.batch_id:
            farm_services.update_batch(
                actor=actor,
                batch_id=visit.batch_id,
                changes={"health_status": HealthStatus(visit.health_status).label},
            )
        if visit.request_id:
            request_obj = VetRequest.objects.select_for_update(of=("self",)).select_related("household__owner").get(
                pk=visit.request_id
            )
            _set_request_in_progress(request_obj, actor, vet=actor)
        events.visit_concluded(visit)
    return visit


def cancel_visit(*, actor: User, pk: int, reason: str) -> VetVisit:
    with transaction.atomic():
        visit = _lock_visit(pk)
        if visit.status != VetStatus.PLANNED:
            raise _conflict(visit, "Tekshiruv")
        visit.status = VetStatus.CANCELLED
        visit.cancel_reason = reason
        visit.save(update_fields=["status", "cancel_reason", "updated_at"])
        _status_change(
            visit, actor, {"status": VetStatus.PLANNED}, {"status": VetStatus.CANCELLED, "cancel_reason": reason}
        )
        events.visit_cancelled(visit)
    return visit


# --- Vaccinations (§8.2) -------------------------------------------------------------------------


def _lock_vaccination(pk: int) -> Vaccination:
    return (
        Vaccination.objects.select_for_update(of=("self",))
        .select_related("household__owner", "batch", "vet", "product")
        .get(pk=pk)
    )


def _vaccine_name(data: dict[str, Any], product: Product | None) -> str:
    name = (data.get("vaccine_name") or "").strip()
    if not name and product is not None:
        name = product.name[:200]
    if len(name) < 2:
        raise ValidationError({"vaccine_name": [MSG_VACCINE_NAME]})
    return name


def plan_vaccinations(*, actor: User, data: dict[str, Any]) -> list[Vaccination]:
    """Single (``household``) or campaign (``household_ids``, one row per household, no batch)."""
    check_date_window(
        data["planned_date"], "planned_date", past=VACCINATION_PAST_DAYS, future=VACCINATION_FUTURE_DAYS
    )
    product = data.get("product")
    check_product(product, {ProductCategory.VACCINE}, MSG_VACCINE_ONLY)
    name = _vaccine_name(data, product)
    vet = resolve_vet(actor, data)
    household_ids = data.get("household_ids")
    with transaction.atomic():
        if household_ids:
            if data.get("batch") is not None:
                raise ValidationError({"batch": [MSG_CAMPAIGN_BATCH]})
            ids = list(dict.fromkeys(household_ids))
            found = {h.pk: h for h in Household.objects.select_related("owner").filter(pk__in=ids)}
            missing = [str(pk) for pk in ids if pk not in found]
            if missing:
                raise ValidationError({"household_ids": [f"Xonadon topilmadi: {', '.join(missing[:5])}"]})
            inactive = [found[pk].code for pk in ids if not found[pk].is_active]
            if inactive:
                raise ValidationError({"household_ids": [f"{MSG_HOUSEHOLD_INACTIVE}: {', '.join(inactive[:5])}"]})
            households = [found[pk] for pk in ids]
            batch = None
        else:
            household = data["household"]
            check_household_active(household)
            batch = data.get("batch")
            check_batch(batch, household)
            households = [household]
        created = []
        for household in households:
            vaccination = Vaccination.objects.create(
                household=household,
                batch=batch,
                vet=vet,
                product=product,
                vaccine_name=name,
                dose=data.get("dose", ""),
                planned_date=data["planned_date"],
                birds_planned=data.get("birds_planned"),
                note=data.get("note", ""),
                created_by=actor,
            )
            audit.record_create(AuditModule.VET, vaccination, actor)
            events.vaccination_planned(vaccination)
            created.append(vaccination)
    return created


VACCINATION_EDITABLE = ("planned_date", "dose", "birds_planned", "vet", "note", "vaccine_name", "product", "batch")


def update_vaccination(*, actor: User, pk: int, changes: dict[str, Any]) -> Vaccination:
    with transaction.atomic():
        vaccination = _lock_vaccination(pk)
        if vaccination.status != VetStatus.PLANNED:
            raise _conflict(vaccination, "Emlash")
        _vet_edit_check(vaccination, actor, changes, MSG_OTHER_VET_VACCINATION)
        if "planned_date" in changes:
            check_date_window(
                changes["planned_date"], "planned_date", past=VACCINATION_PAST_DAYS, future=VACCINATION_FUTURE_DAYS
            )
        # The edit form re-sends unchanged values: only a NEW product / batch is validated again (an unchanged,
        # meanwhile deactivated product must not block editing the date or the dose).
        if "product" in changes and _pk(changes["product"]) != vaccination.product_id:
            check_product(changes["product"], {ProductCategory.VACCINE}, MSG_VACCINE_ONLY)
        if "batch" in changes and _pk(changes["batch"]) != vaccination.batch_id:
            check_batch(changes["batch"], vaccination.household)
        if "vet" in changes:
            check_vet(changes["vet"])
        if "vaccine_name" in changes:
            product = changes.get("product", vaccination.product)
            changes["vaccine_name"] = _vaccine_name(changes, product)
        before = audit.snapshot(vaccination)
        old_key = events.vaccination_key(vaccination)
        for name in VACCINATION_EDITABLE:
            if name in changes:
                setattr(vaccination, name, changes[name])
        vaccination.save()
        audit.record_update(AuditModule.VET, vaccination, actor, before)
        if events.vaccination_key(vaccination) != old_key:
            events.vaccination_planned(vaccination)  # new date / vet: owner and vet learn the new plan
    return vaccination


def complete_vaccination(*, actor: User, pk: int, data: dict[str, Any]) -> Vaccination:
    """PLANNED → DONE; with a product and ``quantity_used > 0`` exactly one stock OUT (§8.2)."""
    today = _today()
    with transaction.atomic():
        vaccination = _lock_vaccination(pk)
        if vaccination.status != VetStatus.PLANNED:
            raise _conflict(vaccination, "Emlash")
        _assigned_check(vaccination, actor, MSG_OTHER_VET_VACCINATION)
        done_date: date = data["done_date"]
        if done_date > today:
            raise ValidationError({"done_date": [MSG_FUTURE_DATE]})
        if done_date < vaccination.planned_date - timedelta(days=DONE_DATE_SLACK_DAYS):
            raise ValidationError({"done_date": [MSG_DATE_RANGE]})
        quantity: Decimal | None = data.get("quantity_used")
        vaccination.status = VetStatus.DONE
        vaccination.done_date = done_date
        vaccination.birds_vaccinated = data["birds_vaccinated"]
        vaccination.quantity_used = quantity
        vaccination.result = data["result"]
        if "note" in data:
            vaccination.note = data["note"]
        if vaccination.product_id and quantity and quantity > 0 and vaccination.stock_transaction_id is None:
            vaccination.stock_transaction = _stock_out(
                actor=actor,
                product=vaccination.product,
                quantity=quantity,
                household=vaccination.household,
                document_no=vaccination.code,
                note=f"Emlash: {vaccination.vaccine_name}",
                reference=("vaccination", vaccination.pk),
            )
        vaccination.save()
        _status_change(
            vaccination,
            actor,
            {"status": VetStatus.PLANNED},
            {
                "status": VetStatus.DONE,
                "vet_id": vaccination.vet_id,
                "done_date": vaccination.done_date,
                "birds_vaccinated": vaccination.birds_vaccinated,
                "quantity_used": vaccination.quantity_used,
                "stock_op_id": vaccination.stock_transaction.op_id if vaccination.stock_transaction_id else None,
            },
        )
        files.bind_files(
            data.get("file_ids") or [], actor=actor, purpose=AttachmentPurpose.VACCINATION, obj=vaccination
        )
        events.vaccination_done(vaccination)
    return vaccination


def cancel_vaccination(*, actor: User, pk: int, reason: str) -> Vaccination:
    with transaction.atomic():
        vaccination = _lock_vaccination(pk)
        if vaccination.status != VetStatus.PLANNED:
            raise _conflict(vaccination, "Emlash")
        vaccination.status = VetStatus.CANCELLED
        vaccination.cancel_reason = reason
        vaccination.save(update_fields=["status", "cancel_reason", "updated_at"])
        _status_change(
            vaccination,
            actor,
            {"status": VetStatus.PLANNED},
            {"status": VetStatus.CANCELLED, "cancel_reason": reason},
        )
        events.vaccination_cancelled(vaccination)
    return vaccination


# --- Diseases (§8.3) -----------------------------------------------------------------------------


def register_disease(*, actor: User, data: dict[str, Any]) -> DiseaseRecord:
    household: Household = data["household"]
    if data["diagnosed_date"] > _today():
        raise ValidationError({"diagnosed_date": [MSG_FUTURE_DATE]})
    with transaction.atomic():
        batch = data.get("batch")
        check_batch(batch, household)
        visit: VetVisit | None = data.get("visit")
        if visit is not None and visit.household_id != household.pk:
            raise ValidationError({"visit": [MSG_VISIT_OTHER_HOUSEHOLD]})
        vet = resolve_vet(actor, data)
        disease = DiseaseRecord.objects.create(
            household=household,
            batch=batch,
            vet=vet,
            visit=visit,
            disease_name=data["disease_name"],
            diagnosed_date=data["diagnosed_date"],
            cases=data["cases"],
            symptoms=data.get("symptoms", ""),
            notes=data.get("notes", ""),
            created_by=actor,
        )
        audit.record_create(AuditModule.VET, disease, actor)
        files.bind_files(data.get("file_ids") or [], actor=actor, purpose=AttachmentPurpose.DISEASE, obj=disease)
        events.disease_registered(disease)
    return disease


DISEASE_EDITABLE = ("status", "resolved_date", "cases", "notes", "symptoms")


def update_disease(*, actor: User, pk: int, changes: dict[str, Any]) -> DiseaseRecord:
    """PATCH; RESOLVED is final (``409``). RESOLVED without a date gets today."""
    with transaction.atomic():
        disease = (
            DiseaseRecord.objects.select_for_update(of=("self",)).select_related("household__owner", "batch", "vet").get(pk=pk)
        )
        if disease.status == DiseaseStatus.RESOLVED:
            raise _conflict(disease, "Kasallik")
        changes = dict(changes)
        new_status = changes.get("status", disease.status)
        if new_status == DiseaseStatus.RESOLVED:
            resolved = changes.get("resolved_date") or _today()
            if resolved > _today():
                raise ValidationError({"resolved_date": [MSG_FUTURE_DATE]})
            if resolved < disease.diagnosed_date:
                raise ValidationError({"resolved_date": [MSG_RESOLVED_BEFORE]})
            changes["resolved_date"] = resolved
        elif changes.get("resolved_date") is not None:
            raise ValidationError({"resolved_date": ["Tuzalgan sana faqat “Tuzaldi” holatida kiritiladi."]})
        else:
            changes.pop("resolved_date", None)
        before = audit.snapshot(disease)
        old_status = disease.status
        for name in DISEASE_EDITABLE:
            if name in changes:
                setattr(disease, name, changes[name])
        disease.save()
        if disease.status != old_status:
            old, new = audit.diff(before, audit.snapshot(disease))
            _status_change(disease, actor, old, new)
        else:
            audit.record_update(AuditModule.VET, disease, actor, before)
    return disease


# --- Treatments (§8.4) ---------------------------------------------------------------------------


def prescribe_treatment(*, actor: User, data: dict[str, Any]) -> Treatment:
    """VET: treatment / recommendation; with product + quantity an OUT ``DV-…``; disease OPEN → TREATMENT."""
    household: Household = data["household"]
    today = _today()
    if data["start_date"] > today + timedelta(days=TREATMENT_FUTURE_DAYS):
        raise ValidationError({"start_date": [MSG_DATE_RANGE]})
    end_date = data.get("end_date")
    if end_date is not None and end_date < data["start_date"]:
        raise ValidationError({"end_date": [MSG_END_BEFORE_START]})
    product = data.get("product")
    check_product(product, {ProductCategory.VET_DRUG, ProductCategory.VACCINE}, MSG_DRUG_ONLY)
    drug_name = (data.get("drug_name") or "").strip() or (product.name[:200] if product is not None else "")
    if len(drug_name) < 2:
        raise ValidationError({"drug_name": [MSG_DRUG_NAME]})
    with transaction.atomic():
        disease: DiseaseRecord | None = data.get("disease")
        if disease is not None:
            disease = DiseaseRecord.objects.select_for_update().get(pk=disease.pk)
            if disease.household_id != household.pk:
                raise ValidationError({"disease": [MSG_DISEASE_OTHER_HOUSEHOLD]})
            if disease.status == DiseaseStatus.RESOLVED:
                raise ValidationError({"disease": [MSG_DISEASE_RESOLVED]})
        batch = data.get("batch")
        if batch is None and disease is not None:
            batch = disease.batch
        check_batch(batch, household)
        treatment = Treatment.objects.create(
            household=household,
            batch=batch,
            disease=disease,
            vet=actor,
            product=product,
            drug_name=drug_name,
            dosage=data.get("dosage", ""),
            start_date=data["start_date"],
            end_date=end_date,
            quantity_used=data.get("quantity_used"),
            instructions=data.get("instructions", ""),
            created_by=actor,
        )
        quantity = treatment.quantity_used
        if product is not None and quantity and quantity > 0:
            treatment.stock_transaction = _stock_out(
                actor=actor,
                product=product,
                quantity=quantity,
                household=household,
                document_no=treatment.code,
                note=f"Davolash: {drug_name}",
                reference=("treatment", treatment.pk),
            )
            treatment.save(update_fields=["stock_transaction", "updated_at"])
        audit.record_create(AuditModule.VET, treatment, actor)
        files.bind_files(data.get("file_ids") or [], actor=actor, purpose=AttachmentPurpose.TREATMENT, obj=treatment)
        if disease is not None and disease.status == DiseaseStatus.OPEN:
            disease.status = DiseaseStatus.TREATMENT
            disease.save(update_fields=["status", "updated_at"])
            _status_change(
                disease, actor, {"status": DiseaseStatus.OPEN}, {"status": DiseaseStatus.TREATMENT, "by": treatment.code}
            )
        events.treatment_prescribed(treatment)
    return treatment


TREATMENT_EDITABLE = ("end_date", "instructions")


def update_treatment(*, actor: User, pk: int, changes: dict[str, Any]) -> Treatment:
    with transaction.atomic():
        treatment = Treatment.objects.select_for_update().get(pk=pk)
        if "end_date" in changes and changes["end_date"] is not None and changes["end_date"] < treatment.start_date:
            raise ValidationError({"end_date": [MSG_END_BEFORE_START]})
        before = audit.snapshot(treatment)
        for name in TREATMENT_EDITABLE:
            if name in changes:
                setattr(treatment, name, changes[name])
        treatment.save()
        audit.record_update(AuditModule.VET, treatment, actor, before)
    return treatment


# --- Owner requests (§8.5) -----------------------------------------------------------------------


def _lock_request(pk: int) -> VetRequest:
    return VetRequest.objects.select_for_update(of=("self",)).select_related("household__owner", "vet").get(pk=pk)


def create_request(*, actor: User, text: str, file_ids: list[int]) -> VetRequest:
    """OWNER: household forced own; at most 5 open requests per household (checked under the household lock)."""
    household = farm_services.household_of(actor)
    with transaction.atomic():
        household = Household.objects.select_for_update(of=("self",)).select_related("owner").get(pk=household.pk)
        open_count = VetRequest.objects.filter(
            household=household, status__in=[RequestStatus.NEW, RequestStatus.IN_PROGRESS]
        ).count()
        if open_count >= MAX_OPEN_REQUESTS:
            raise ValidationError({"non_field_errors": [MSG_OPEN_REQUESTS]})
        request_obj = VetRequest.objects.create(household=household, created_by=actor, text=text)
        audit.record_create(AuditModule.VET, request_obj, actor)
        files.bind_files(file_ids, actor=actor, purpose=AttachmentPurpose.VET_REQUEST, obj=request_obj)
        events.request_created(request_obj)
    return request_obj


def take_request(*, actor: User, pk: int, response: str = "") -> VetRequest:
    with transaction.atomic():
        request_obj = _lock_request(pk)
        if request_obj.status != RequestStatus.NEW:
            raise _conflict(request_obj, "Murojaat")
        request_obj.status = RequestStatus.IN_PROGRESS
        request_obj.vet = actor
        if response:
            request_obj.response = response
        request_obj.save(update_fields=["status", "vet", "response", "updated_at"])
        _status_change(
            request_obj,
            actor,
            {"status": RequestStatus.NEW},
            {"status": RequestStatus.IN_PROGRESS, "vet_id": actor.pk, "response": request_obj.response},
        )
        events.request_updated(request_obj)
    return request_obj


def close_request(*, actor: User, pk: int, response: str | None) -> VetRequest:
    """Staff close with a response (owner is notified); the owner may withdraw his own NEW request."""
    owner = role_of(actor) == Role.OWNER
    response = (response or "").strip()
    if not owner and len(response) < 3:
        raise ValidationError({"response": [MSG_RESPONSE_REQUIRED]})
    with transaction.atomic():
        request_obj = _lock_request(pk)
        if request_obj.status == RequestStatus.CLOSED or (owner and request_obj.status != RequestStatus.NEW):
            raise _conflict(request_obj, "Murojaat")
        old_status = request_obj.status
        request_obj.status = RequestStatus.CLOSED
        request_obj.response = OWNER_CLOSE_RESPONSE if owner else (response or "")
        request_obj.closed_at = timezone.now()
        request_obj.closed_by = actor
        if not owner and request_obj.vet_id is None and role_of(actor) == Role.VET:
            request_obj.vet = actor
        request_obj.save(update_fields=["status", "response", "closed_at", "closed_by", "vet", "updated_at"])
        _status_change(
            request_obj,
            actor,
            {"status": old_status},
            {"status": RequestStatus.CLOSED, "response": request_obj.response},
        )
        if not owner:
            events.request_updated(request_obj)
    return request_obj


# --- Delete (soft) -------------------------------------------------------------------------------

MSG_DELETE_OTHER_VET = "Bu yozuv boshqa veterinarga tegishli. Uni faqat administrator o‘chira oladi."
MSG_DELETE_REQUEST_ADMIN_ONLY = "Murojaatni faqat administrator o‘chira oladi."

_DELETABLE: dict[str, type] = {
    "visit": VetVisit,
    "vaccination": Vaccination,
    "disease": DiseaseRecord,
    "request": VetRequest,
}


def can_delete(actor: User, obj: VetVisit | Vaccination | DiseaseRecord | VetRequest) -> bool:
    """ADMIN — any row; VET — his own visit / vaccination / disease record (assigned to him, or unassigned and
    entered by him). Owner requests — ADMIN only (the owner withdraws his own NEW request with ``close``)."""
    role = role_of(actor)
    if role == Role.ADMIN:
        return True
    if role != Role.VET or isinstance(obj, VetRequest):
        return False
    if obj.vet_id is not None:
        return obj.vet_id == actor.pk
    return obj.created_by_id == actor.pk


def delete_record(*, actor: User, kind: str, pk: int) -> None:
    """Soft delete a visit / vaccination / disease record / owner request (``deleted_at``; the row stays as history).

    Nothing else changes: a DONE vaccination keeps its stock OUT (the ledger is never touched), linked rows
    (diseases of a visit, treatments of a disease, visits of a request) keep pointing at the hidden row. The row
    disappears from lists, details (404), calendar, summaries, dashboards, reports, reminders, search and acts.
    """
    model = _DELETABLE[kind]
    with transaction.atomic():
        obj = model.all_objects.select_for_update(of=("self",)).get(pk=pk)
        if obj.deleted_at is not None:
            return
        if not can_delete(actor, obj):
            raise PermissionDenied(MSG_DELETE_REQUEST_ADMIN_ONLY if model is VetRequest else MSG_DELETE_OTHER_VET)
        before = audit.snapshot(obj)
        obj.deleted_at = timezone.now()
        obj.save(update_fields=["deleted_at", "updated_at"])
        audit.record(
            module=AuditModule.VET,
            action=AuditAction.DELETE,
            obj=obj,
            actor=actor,
            before=before,
            after={"deleted": True},
        )

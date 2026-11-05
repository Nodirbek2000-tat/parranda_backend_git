"""Audit snapshots of the vet objects (business fields only; ``op_id`` = the vet code, e.g. ``VT-000004``)."""

from __future__ import annotations

from typing import Any

from audit import services as audit
from audit.models import AuditObjectType

from .models import DiseaseRecord, Treatment, Vaccination, VetRequest, VetVisit


def _visit(v: VetVisit) -> dict[str, Any]:
    return {
        "code": v.code,
        "household_id": v.household_id,
        "batch_id": v.batch_id,
        "vet_id": v.vet_id,
        "planned_date": v.planned_date,
        "purpose": v.purpose,
        "status": v.status,
        "done_at": v.done_at,
        "health_status": v.health_status,
        "birds_checked": v.birds_checked,
        "conclusion": v.conclusion,
        "request_id": v.request_id,
        "cancel_reason": v.cancel_reason,
    }


def _vaccination(v: Vaccination) -> dict[str, Any]:
    return {
        "code": v.code,
        "household_id": v.household_id,
        "batch_id": v.batch_id,
        "vet_id": v.vet_id,
        "product_id": v.product_id,
        "vaccine_name": v.vaccine_name,
        "dose": v.dose,
        "planned_date": v.planned_date,
        "birds_planned": v.birds_planned,
        "status": v.status,
        "done_date": v.done_date,
        "birds_vaccinated": v.birds_vaccinated,
        "quantity_used": v.quantity_used,
        "note": v.note,
        "cancel_reason": v.cancel_reason,
    }


def _disease(d: DiseaseRecord) -> dict[str, Any]:
    return {
        "code": d.code,
        "household_id": d.household_id,
        "batch_id": d.batch_id,
        "vet_id": d.vet_id,
        "visit_id": d.visit_id,
        "disease_name": d.disease_name,
        "diagnosed_date": d.diagnosed_date,
        "cases": d.cases,
        "symptoms": d.symptoms,
        "status": d.status,
        "resolved_date": d.resolved_date,
        "notes": d.notes,
    }


def _treatment(t: Treatment) -> dict[str, Any]:
    return {
        "code": t.code,
        "household_id": t.household_id,
        "batch_id": t.batch_id,
        "disease_id": t.disease_id,
        "vet_id": t.vet_id,
        "product_id": t.product_id,
        "drug_name": t.drug_name,
        "dosage": t.dosage,
        "start_date": t.start_date,
        "end_date": t.end_date,
        "quantity_used": t.quantity_used,
        "instructions": t.instructions,
        "stock_transaction_id": t.stock_transaction_id,
    }


def _request(r: VetRequest) -> dict[str, Any]:
    return {
        "code": r.code,
        "household_id": r.household_id,
        "text": r.text,
        "status": r.status,
        "vet_id": r.vet_id,
        "response": r.response,
    }


def register() -> None:
    audit.register_snapshot(VetVisit, AuditObjectType.VET_VISIT, _visit, op_id_attr="code")
    audit.register_snapshot(Vaccination, AuditObjectType.VACCINATION, _vaccination, op_id_attr="code")
    audit.register_snapshot(DiseaseRecord, AuditObjectType.DISEASE_RECORD, _disease, op_id_attr="code")
    audit.register_snapshot(Treatment, AuditObjectType.TREATMENT, _treatment, op_id_attr="code")
    audit.register_snapshot(VetRequest, AuditObjectType.VET_REQUEST, _request, op_id_attr="code")

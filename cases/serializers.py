"""JSON shapes of cases (plain functions: easy to read, easy to keep in sync with docs/API_V2_NOTES.md)."""

from decimal import Decimal

from django.core import signing
from rest_framework import serializers

from stock.models import Package

from accounts.models import Role

from . import stages as chain
from .models import CaseStatus, RequestKind, StageStatus
from .services import (NEXT_CYCLE_CHOICES, can_flags, can_start_next_cycle, default_next_cycle_stage,
                       household_cycles)

FILE_SIGNER = signing.TimestampSigner(salt="nnp.files")
FILE_URL_MAX_AGE = 12 * 3600


def file_url(kind: str, pk: int) -> str:
    """API path of a private file with a short-lived signature, usable in <img src>."""
    return f"/api/files/{kind}/{pk}?sig={FILE_SIGNER.sign(f'{kind}-{pk}')}"


def user_brief(user):
    if user is None:
        return None
    return {"id": user.pk, "full_name": user.full_name, "role": user.role, "role_display": user.get_role_display()}


def _num(value):
    return None if value is None else float(value)


def photo_json(photo):
    return {
        "id": photo.pk, "stage": photo.stage, "request": photo.request_id, "visit": photo.visit_id,
        "url": file_url("photo", photo.pk),
        "taken_at": photo.taken_at, "lat": _num(photo.lat), "lng": _num(photo.lng), "live": photo.live,
        "uploaded_by": user_brief(photo.uploaded_by),
    }


def document_json(doc):
    return {"id": doc.pk, "stage": doc.stage, "url": file_url("document", doc.pk), "original_name": doc.original_name,
            "uploaded_by": user_brief(doc.uploaded_by)}


def event_json(event, with_case=False):
    row = {
        "id": event.pk, "stage_number": event.stage_number, "stage_title": chain.get(event.stage_number).title,
        "action": event.action, "action_display": event.get_action_display(),
        "actor": user_brief(event.actor), "note": event.note, "created_at": event.created_at,
    }
    if with_case:
        row["case"] = {"id": event.case_id, "code": event.case.code, "name": event.case.name}
    return row


def _current_stage(case):
    stages = getattr(case, "_prefetched_objects_cache", {}).get("stages")
    if stages is not None:
        return next(s for s in stages if s.number == case.current_stage)
    return case.stages.get(number=case.current_stage)


def case_item(case):
    stage = _current_stage(case)
    in_progress = case.status == CaseStatus.IN_PROGRESS
    return {
        "id": case.pk, "code": case.code, "cycle": case.cycle, "name": case.name, "owner_full_name": case.owner_full_name,
        "owner_phone": case.owner_phone, "region": case.region, "district": case.district, "mfy": case.mfy,
        "latitude": float(case.latitude) if case.latitude is not None else None,
        "longitude": float(case.longitude) if case.longitude is not None else None,
        "package": {"id": case.package_id, "name": case.package.name},
        "status": case.status, "status_display": case.get_status_display(),
        "current_stage": case.current_stage, "current_stage_title": chain.get(case.current_stage).title,
        "current_stage_status": stage.status, "current_stage_status_display": stage.get_status_display(),
        "due_at": stage.due_at if in_progress else None,
        "is_overdue": in_progress and stage.is_overdue,
        "created_at": case.created_at,
    }


def stage_json(stage, photos, documents):
    d = chain.get(stage.number)
    return {
        "number": stage.number, "key": stage.key, "title": d.title,
        "executor_role": d.executor, "approver_role": d.approver, "days": chain.days_of(stage.number),
        "min_photos": d.photos,
        "fields": [f.as_dict() for f in d.fields],
        "status": stage.status, "status_display": stage.get_status_display(),
        "data": stage.data, "note": stage.note,
        "activated_at": stage.activated_at, "due_at": stage.due_at, "is_overdue": stage.is_overdue,
        "submitted_by": user_brief(stage.submitted_by), "submitted_at": stage.submitted_at,
        "decided_by": user_brief(stage.decided_by), "decided_at": stage.decided_at,
        "return_reason": stage.return_reason,
        "has_act": chain.has_act(stage.number) and stage.status == StageStatus.DONE and not (stage.data or {}).get("carried_over"),
        "photos": [photo_json(p) for p in photos if p.stage == stage.number],
        "documents": [document_json(doc) for doc in documents if doc.stage == stage.number],
    }


def case_detail(case, user):
    from .finance import case_finance, has_cost
    from .growing import growing_info, visible_requests, visible_visits

    stages = list(case.stages.select_related("submitted_by", "decided_by"))
    case._prefetched_objects_cache = {**getattr(case, "_prefetched_objects_cache", {}), "stages": stages}
    photos = list(case.photos.filter(stage__isnull=False).select_related("uploaded_by"))
    documents = list(case.documents.select_related("uploaded_by"))
    current = next(s for s in stages if s.number == case.current_stage)
    data = case_item(case)
    data.update({
        "mfy": case.mfy, "address": case.address,
        "latitude": _num(case.latitude), "longitude": _num(case.longitude),
        "package": {"id": case.package_id, "name": case.package.name, "area_sotix": float(case.package.area_sotix),
                    "capacity_birds": case.package.capacity_birds},
        "owner_user": None if case.owner_user is None else {
            "id": case.owner_user.pk, "username": case.owner_user.username, "full_name": case.owner_user.full_name},
        "created_by": user_brief(case.created_by),
        "done_at": case.done_at, "reject_reason": case.reject_reason,
        "stages": [stage_json(s, photos, documents) for s in stages],
        "photos": [photo_json(p) for p in photos],
        "documents": [document_json(doc) for doc in documents],
        "events": [event_json(e) for e in case.events.select_related("actor")],
        "can": can_flags(user, case, current),
        "vet_user": user_brief(case.vet_user),
        "growing": growing_info(case, next((s for s in stages if s.number == chain.GROWING_STAGE), None)),
        "cycles": [{"id": c.pk, "code": c.code, "cycle": c.cycle, "status": c.status} for c in household_cycles(case)],
        "next_cycle": {
            "can": can_start_next_cycle(user, case),
            "default_stage": default_next_cycle_stage(),
            "options": [{"number": n, "title": chain.get(n).title} for n in NEXT_CYCLE_CHOICES],
        },
    })
    growing = data["can"]["create_request"] or growing_period(case, stages)
    requests = request_queryset(visible_requests(user).filter(case=case))
    visits = visit_queryset(visible_visits(user).filter(case=case)).order_by("planned_date", "id")
    fin = None
    if user.role != Role.OWNER:
        fin = case_finance(case)
        fin = fin if has_cost(fin) else None
    data.update({
        "requests": [request_json(r, user, growing=growing) for r in requests],
        "visits": [visit_json(v, user) for v in visits],
        "finance": fin,
    })
    return data


def growing_period(case, stages) -> bool:
    from .services import in_growing_period

    stage = next((s for s in stages if s.number == chain.GROWING_STAGE), None)
    return stage is not None and in_growing_period(case, stage)


# --- v2.1: requests and visits ------------------------------------------------------------------

def request_queryset(qs):
    return qs.select_related(
        "case", "visit", "created_by", "logistics_by", "admin_by", "delivered_by", "done_by", "rejected_by",
        "delivered_product").prefetch_related("photos__uploaded_by")


def visit_queryset(qs):
    return qs.select_related("case", "vet", "cancelled_by").prefetch_related(
        "photos__uploaded_by", "drugs__product")


def _case_brief(case):
    return {"id": case.pk, "code": case.code, "name": case.name, "status": case.status,
            "current_stage": case.current_stage}


def _qty_num(value):
    return None if value is None else float(value)


def request_json(req, user, growing=None):
    from .growing import request_can

    try:
        visit_id = req.visit.pk
    except Exception:  # no visit (reverse one-to-one)
        visit_id = None
    delivery_ids = set(req.delivery_photo_ids or [])
    photos = list(req.photos.all())
    delivery = None
    if req.delivered_product_id:
        product = req.delivered_product
        delivery = {
            "product": {"id": product.pk, "name": product.name, "unit": product.unit},
            "product_id": product.pk, "product_name": product.name, "unit": product.unit,
            "qty": _qty_num(req.delivered_qty), "note": req.delivery_note,
            "photos": [photo_json(p) for p in photos if p.pk in delivery_ids],
        }
    return {
        "id": req.pk, "kind": req.kind, "kind_display": req.get_kind_display(),
        "status": req.status, "status_display": req.get_status_display(),
        "text": req.text, "qty_kg": _qty_num(req.qty_kg),
        "case": _case_brief(req.case),
        "created_by": user_brief(req.created_by), "created_at": req.created_at, "updated_at": req.updated_at,
        "logistics_by": user_brief(req.logistics_by), "logistics_at": req.logistics_at,
        "admin_by": user_brief(req.admin_by), "admin_at": req.admin_at,
        "delivered_by": user_brief(req.delivered_by), "delivered_at": req.delivered_at,
        "delivery": delivery,
        "done_by": user_brief(req.done_by), "done_at": req.done_at,
        "rejected_by": user_brief(req.rejected_by), "rejected_at": req.rejected_at,
        "reject_reason": req.reject_reason,
        "visit_id": visit_id,
        "photos": [photo_json(p) for p in photos if p.pk not in delivery_ids],
        "can": request_can(user, req, growing=growing),
    }


def visit_json(visit, user):
    from .growing import visit_can

    health = dict(chain.HEALTH_CHOICES)
    drugs = []
    for d in visit.drugs.all():
        price = d.product.price
        drugs.append({"product_id": d.product_id, "product_name": d.product.name, "unit": d.product.unit,
                      "qty": float(d.qty), "price": float(price), "cost": int(round(d.qty * price))})
    return {
        "id": visit.pk, "case": _case_brief(visit.case), "vet": user_brief(visit.vet),
        "kind": visit.kind, "kind_display": visit.get_kind_display(), "week": visit.week,
        "request": visit.request_id, "request_id": visit.request_id,
        "planned_date": visit.planned_date, "status": visit.status, "status_display": visit.get_status_display(),
        "is_overdue": visit.is_overdue, "done_at": visit.done_at, "chicks_count": visit.chicks_count,
        "health": visit.health, "health_display": health.get(visit.health, ""), "note": visit.note,
        "cancel_reason": visit.cancel_reason, "cancelled_by": user_brief(visit.cancelled_by),
        "cancelled_at": visit.cancelled_at, "created_at": visit.created_at,
        "photos": [photo_json(p) for p in visit.photos.all()],
        "drugs": drugs,
        "can": visit_can(user, visit),
    }


# --- Input --------------------------------------------------------------------------------------

class IdList(serializers.ListField):
    child = serializers.IntegerField(min_value=1)

    def __init__(self, **kwargs):
        kwargs.setdefault("required", False)
        kwargs.setdefault("default", list)
        super().__init__(**kwargs)


class HouseholdMixin(serializers.Serializer):
    name = serializers.CharField(max_length=200)
    owner_full_name = serializers.CharField(max_length=200)
    owner_phone = serializers.CharField(max_length=30, required=False, allow_blank=True, default="")
    region = serializers.CharField(max_length=100, required=False, allow_blank=True, default="")
    district = serializers.CharField(max_length=100, required=False, allow_blank=True, default="")
    mfy = serializers.CharField(max_length=150, required=False, allow_blank=True, default="")
    address = serializers.CharField(max_length=300, required=False, allow_blank=True, default="")
    latitude = serializers.FloatField(min_value=-90, max_value=90)
    longitude = serializers.FloatField(min_value=-180, max_value=180)


class CaseCreateSerializer(HouseholdMixin):
    package_id = serializers.PrimaryKeyRelatedField(queryset=Package.objects.all(), source="package")
    conclusion = serializers.CharField(max_length=4000)
    photo_ids = IdList()
    document_ids = IdList()


class SubmitSerializer(serializers.Serializer):
    data = serializers.DictField(required=False, default=dict)
    note = serializers.CharField(required=False, allow_blank=True, default="", max_length=4000)
    photo_ids = IdList()
    document_ids = IdList()


class ReasonSerializer(serializers.Serializer):
    reason = serializers.CharField(max_length=4000)


class NoteSerializer(serializers.Serializer):
    note = serializers.CharField(required=False, allow_blank=True, default="", max_length=4000)


class RequestCreateSerializer(serializers.Serializer):
    kind = serializers.ChoiceField(choices=RequestKind.choices)
    text = serializers.CharField(max_length=2000)
    qty_kg = serializers.DecimalField(max_digits=10, decimal_places=2, min_value=Decimal("0.01"), required=False,
                                      allow_null=True, default=None)
    photo_ids = IdList()


class DeliverSerializer(serializers.Serializer):
    product_id = serializers.IntegerField(min_value=1)
    qty = serializers.DecimalField(max_digits=14, decimal_places=3, min_value=Decimal("0.001"))
    photo_ids = IdList()
    note = serializers.CharField(required=False, allow_blank=True, default="", max_length=2000)


class DrugSerializer(serializers.Serializer):
    product_id = serializers.IntegerField(min_value=1)
    qty = serializers.DecimalField(max_digits=14, decimal_places=3, min_value=Decimal("0.001"))


class VisitCompleteSerializer(serializers.Serializer):
    chicks_count = serializers.IntegerField(min_value=0)
    health = serializers.ChoiceField(choices=chain.HEALTH_CHOICES)
    note = serializers.CharField(required=False, allow_blank=True, default="", max_length=4000)
    photo_ids = IdList()
    drugs = DrugSerializer(many=True, required=False, default=list)


class AssignVetSerializer(serializers.Serializer):
    vet_id = serializers.IntegerField(min_value=1, allow_null=True)


STATUS_CHOICES = {c for c, _ in CaseStatus.choices}
STAGE_STATUS_CHOICES = {c for c, _ in StageStatus.choices}

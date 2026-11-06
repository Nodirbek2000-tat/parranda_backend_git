"""JSON shapes of cases (plain functions: easy to read, easy to keep in sync with docs/API_V2_NOTES.md)."""

from django.core import signing
from rest_framework import serializers

from stock.models import Package

from . import stages as chain
from .models import CaseStatus, StageStatus
from .services import can_flags

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
        "id": photo.pk, "stage": photo.stage, "url": file_url("photo", photo.pk),
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
        "id": case.pk, "code": case.code, "name": case.name, "owner_full_name": case.owner_full_name,
        "owner_phone": case.owner_phone, "region": case.region, "district": case.district,
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
        "executor_role": d.executor, "approver_role": d.approver, "days": d.days, "min_photos": d.photos,
        "fields": [f.as_dict() for f in d.fields],
        "status": stage.status, "status_display": stage.get_status_display(),
        "data": stage.data, "note": stage.note,
        "activated_at": stage.activated_at, "due_at": stage.due_at, "is_overdue": stage.is_overdue,
        "submitted_by": user_brief(stage.submitted_by), "submitted_at": stage.submitted_at,
        "decided_by": user_brief(stage.decided_by), "decided_at": stage.decided_at,
        "return_reason": stage.return_reason,
        "photos": [photo_json(p) for p in photos if p.stage == stage.number],
        "documents": [document_json(doc) for doc in documents if doc.stage == stage.number],
    }


def case_detail(case, user):
    stages = list(case.stages.select_related("submitted_by", "decided_by"))
    photos = list(case.photos.select_related("uploaded_by"))
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
    })
    return data


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


STATUS_CHOICES = {c for c, _ in CaseStatus.choices}
STAGE_STATUS_CHOICES = {c for c, _ in StageStatus.choices}

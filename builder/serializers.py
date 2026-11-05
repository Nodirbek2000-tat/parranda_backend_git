"""Serializers of the Konstruktor API (API_CONTRACT_V4 §1–§3)."""

from __future__ import annotations

from typing import Any

from drf_spectacular.utils import extend_schema_field
from rest_framework import serializers

from common.roles import role_display
from core.models import Attachment

from . import schemas, services
from .models import Blueprint, BlueprintKind, ConfigVersion, FormSubmission, ProcessRun, TaskInstance

MSG_VERSION_NOT_FOUND = services.MSG_VERSION_NOT_FOUND
MAX_ID = 2**63 - 1


def _name(user) -> str | None:
    return user.full_name if user is not None else None


def household_ref(household) -> dict | None:
    if household is None:
        return None
    return {"id": household.pk, "code": household.code, "name": household.name}




class _HouseholdRef(serializers.Serializer):
    id = serializers.IntegerField()
    code = serializers.CharField()
    name = serializers.CharField()


def profile_names() -> dict[int, str]:
    return dict(Blueprint.objects.filter(kind=BlueprintKind.ROLE).values_list("pk", "name"))


def file_names_for(rows: list[tuple[list, dict]]) -> dict[int, str]:
    """``{attachment_id: original_name}`` of every file/photo value in ``(fields, data)`` pairs (one query)."""
    ids: set[int] = set()
    for fields, data in rows:
        for field in fields or ():
            value = (data or {}).get(field.get("key"))
            if field.get("type") in schemas.FILE_TYPES and isinstance(value, int):
                ids.add(value)
    if not ids:
        return {}
    return dict(Attachment.objects.filter(pk__in=ids).values_list("pk", "original_name"))


# --- Blueprints (§1) -----------------------------------------------------------------------------


class BlueprintUsageSerializer(serializers.Serializer):
    users = serializers.IntegerField()
    tasks = serializers.IntegerField()
    submissions = serializers.IntegerField()
    runs = serializers.IntegerField()
    referenced_by = serializers.ListField(child=serializers.DictField())


class BlueprintListItemSerializer(serializers.ModelSerializer):
    kind_display = serializers.CharField(source="get_kind_display", read_only=True)
    status_display = serializers.CharField(source="get_status_display", read_only=True)
    has_pending = serializers.SerializerMethodField()
    pending_by_name = serializers.SerializerMethodField()
    created_by_name = serializers.SerializerMethodField()
    usage = serializers.SerializerMethodField()

    class Meta:
        model = Blueprint
        fields = (
            "id", "kind", "kind_display", "name", "description", "status", "status_display", "version",
            "has_pending", "pending_by_name", "pending_at", "created_by_name", "created_at", "updated_at",
            "activated_at", "usage",
        )
        read_only_fields = fields

    def get_has_pending(self, bp: Blueprint) -> bool:
        return bool(bp.pending)

    def get_pending_by_name(self, bp: Blueprint) -> str | None:
        return _name(bp.pending_by)

    def get_created_by_name(self, bp: Blueprint) -> str | None:
        return _name(bp.created_by)

    @extend_schema_field(BlueprintUsageSerializer)
    def get_usage(self, bp: Blueprint) -> dict:
        usage = self.context.get("usage")
        if usage is None or bp.pk not in usage:
            usage = services.usage_map([bp])
        return usage[bp.pk]


class BlueprintSerializer(BlueprintListItemSerializer):
    data = serializers.JSONField(read_only=True)
    pending = serializers.JSONField(read_only=True)

    class Meta(BlueprintListItemSerializer.Meta):
        fields = tuple(f for f in BlueprintListItemSerializer.Meta.fields if f != "has_pending") + ("data", "pending")
        read_only_fields = fields


class BlueprintCreateSerializer(serializers.Serializer):
    kind = serializers.ChoiceField(choices=BlueprintKind.choices)
    name = serializers.CharField(max_length=200)
    description = serializers.CharField(max_length=1000, required=False, allow_blank=True, default="")
    data = serializers.JSONField()


class BlueprintUpdateSerializer(serializers.Serializer):
    name = serializers.CharField(max_length=200, required=False)
    description = serializers.CharField(max_length=1000, required=False, allow_blank=True)
    data = serializers.JSONField(required=False)
    comment = serializers.CharField(max_length=500, required=False, allow_blank=True)


class CommentSerializer(serializers.Serializer):
    comment = serializers.CharField(max_length=500, required=False, allow_blank=True, default="")


class ConfigVersionSerializer(serializers.ModelSerializer):
    created_by_name = serializers.SerializerMethodField()

    class Meta:
        model = ConfigVersion
        fields = ("id", "version", "name", "status", "comment", "data", "created_by_name", "created_at")
        read_only_fields = fields

    def get_created_by_name(self, row: ConfigVersion) -> str | None:
        return _name(row.created_by)


class RevertSerializer(serializers.Serializer):
    version = serializers.IntegerField(min_value=1, max_value=2**31 - 1)


# --- User access (§2) ----------------------------------------------------------------------------


class UserAccessRowSerializer(serializers.Serializer):
    user_id = serializers.IntegerField(source="pk")
    full_name = serializers.CharField()
    username = serializers.CharField()
    role = serializers.CharField(source="effective_role")
    role_display = serializers.SerializerMethodField()
    profile_id = serializers.SerializerMethodField()
    profile_name = serializers.SerializerMethodField()
    districts = serializers.SerializerMethodField()
    household_ids = serializers.SerializerMethodField()
    households = serializers.SerializerMethodField()

    @staticmethod
    def _access(user):
        try:
            return user.access
        except Exception:  # noqa: BLE001 - RelatedObjectDoesNotExist
            return None

    def get_role_display(self, user) -> str:
        return role_display(user)

    def get_profile_id(self, user) -> int | None:
        access = self._access(user)
        return access.profile_id if access else None

    def get_profile_name(self, user) -> str | None:
        access = self._access(user)
        return access.profile.name if access and access.profile_id else None

    def get_districts(self, user) -> list[str]:
        access = self._access(user)
        return list(access.districts or []) if access else []

    def get_household_ids(self, user) -> list[int]:
        access = self._access(user)
        return [h.pk for h in access.households.all()] if access else []

    @extend_schema_field(_HouseholdRef(many=True))
    def get_households(self, user) -> list[dict]:
        access = self._access(user)
        return [household_ref(h) for h in access.households.all()] if access else []


class UserAccessUpdateSerializer(serializers.Serializer):
    profile_id = serializers.IntegerField(min_value=1, max_value=MAX_ID, allow_null=True, required=False,
                                          default=None)
    districts = serializers.ListField(child=serializers.CharField(max_length=120), required=False, default=list,
                                      max_length=100)
    household_ids = serializers.ListField(child=serializers.IntegerField(min_value=1, max_value=MAX_ID),
                                          required=False, default=list, max_length=1000)

    def validate_districts(self, value: list[str]) -> list[str]:
        return list(dict.fromkeys(item.strip() for item in value if item.strip()))


# --- Forms (§3.1) --------------------------------------------------------------------------------


class FormListItemSerializer(serializers.Serializer):
    id = serializers.IntegerField(source="pk")
    name = serializers.CharField()
    description = serializers.CharField()
    status = serializers.CharField()
    version = serializers.IntegerField()
    fields = serializers.SerializerMethodField(method_name="visible_form_fields")

    def visible_form_fields(self, form: Blueprint) -> list[dict]:
        return schemas.visible_fields((form.data or {}).get("fields", []), self.context["request"].user)


def submission_payload(sub: FormSubmission, user, file_names: dict[int, str]) -> dict[str, Any]:
    fields = schemas.visible_fields(sub.fields or [], user)
    data = sub.data or {}
    display = []
    for field in fields:
        value = data.get(field["key"])
        item = {"key": field["key"], "label": field.get("label", ""), "type": field.get("type", ""),
                "value": schemas.display_value(field, value, file_names)}
        if field.get("type") in schemas.FILE_TYPES:
            item["file_id"] = value if isinstance(value, int) else None
        display.append(item)
    return {
        "id": sub.pk,
        "code": sub.code,
        "form_id": sub.form_id,
        "form_name": sub.form.name,
        "form_version": sub.form_version,
        "fields": fields,
        "data": {field["key"]: data.get(field["key"]) for field in fields if field["key"] in data},
        "display": display,
        "household": household_ref(sub.household),
        "task_id": sub.task_id,
        "is_test": sub.is_test,
        "submitted_by_name": _name(sub.submitted_by),
        "created_at": serializers.DateTimeField().to_representation(sub.created_at),
    }


class SubmissionSerializer(serializers.Serializer):
    """Documentation shape of ``Submission`` (built by :func:`submission_payload`)."""

    id = serializers.IntegerField()
    code = serializers.CharField()
    form_id = serializers.IntegerField()
    form_name = serializers.CharField()
    form_version = serializers.IntegerField()
    fields = serializers.ListField(child=serializers.DictField())
    data = serializers.DictField()
    display = serializers.ListField(child=serializers.DictField())
    household = _HouseholdRef(allow_null=True)
    task_id = serializers.IntegerField(allow_null=True)
    is_test = serializers.BooleanField()
    submitted_by_name = serializers.CharField(allow_null=True)
    created_at = serializers.DateTimeField()


def submissions_payload(rows: list[FormSubmission], user) -> list[dict]:
    names = file_names_for([(row.fields, row.data) for row in rows])
    return [submission_payload(row, user, names) for row in rows]


class SubmissionCreateSerializer(serializers.Serializer):
    household_id = serializers.IntegerField(min_value=1, max_value=MAX_ID, required=False, allow_null=True,
                                            default=None)
    data = serializers.JSONField(required=False, default=dict)


# --- Tasks (§3.2) --------------------------------------------------------------------------------


def task_payload(task: TaskInstance, user, *, profiles: dict[int, str], submission: FormSubmission | None,
                 file_names: dict[int, str]) -> dict[str, Any]:
    definition = task.definition or {}
    form = definition.get("form")
    run = task.process_run
    stage_title = ""
    if run is not None and task.stage_index is not None and task.stage_index < len(run.stages or []):
        stage_title = run.stages[task.stage_index].get("title", "")
    dt = serializers.DateTimeField()
    return {
        "id": task.pk,
        "code": task.code,
        "title": task.title,
        "template_id": task.template_id,
        "template_version": task.template_version,
        "status": task.status,
        "status_display": task.get_status_display(),
        "executor": task.executor,
        "executor_display": services.ref_display(task.executor, profiles),
        "approver": task.approver,
        "approver_display": services.ref_display(task.approver, profiles),
        "assigned_to": {"id": task.assigned_to_id, "full_name": task.assigned_to.full_name}
        if task.assigned_to_id else None,
        "household": household_ref(task.household),
        "process_run": {"id": run.pk, "code": run.code, "stage_index": task.stage_index, "stage_title": stage_title}
        if run is not None else None,
        "starts_on": task.starts_on.isoformat() if task.starts_on else None,
        "due_on": task.due_on.isoformat() if task.due_on else None,
        "overdue": services.is_overdue(task),
        "form": {"id": form.get("id"), "name": form.get("name", ""),
                 "fields": schemas.visible_fields(form.get("fields", []), user)} if form else None,
        "submission": submission_payload(submission, user, file_names) if submission is not None else None,
        "note": definition.get("note", ""),
        "decision_note": task.decision_note,
        "submitted_by_name": _name(task.submitted_by),
        "submitted_at": dt.to_representation(task.submitted_at) if task.submitted_at else None,
        "decided_by_name": _name(task.decided_by),
        "decided_at": dt.to_representation(task.decided_at) if task.decided_at else None,
        "can_submit": services.can_submit(user, task),
        "can_approve": services.can_approve(user, task),
        "can_cancel": services.can_cancel(user, task),
        "is_test": task.is_test,
        "created_at": dt.to_representation(task.created_at),
    }


def tasks_payload(rows: list[TaskInstance], user) -> list[dict]:
    profiles = profile_names()
    latest: dict[int, FormSubmission] = {}
    for sub in FormSubmission.objects.filter(task_id__in=[t.pk for t in rows]).select_related(
        "form", "household", "submitted_by"
    ).order_by("id"):
        latest[sub.task_id] = sub
    names = file_names_for([(sub.fields, sub.data) for sub in latest.values()])
    return [task_payload(t, user, profiles=profiles, submission=latest.get(t.pk), file_names=names) for t in rows]


class TaskSerializer(serializers.Serializer):
    """Documentation shape of ``Task`` (built by :func:`task_payload`)."""

    id = serializers.IntegerField()
    code = serializers.CharField()
    title = serializers.CharField()
    template_id = serializers.IntegerField()
    template_version = serializers.IntegerField()
    status = serializers.CharField()
    status_display = serializers.CharField()
    executor = serializers.CharField()
    executor_display = serializers.CharField()
    approver = serializers.CharField()
    approver_display = serializers.CharField()
    assigned_to = serializers.DictField(allow_null=True)
    household = _HouseholdRef(allow_null=True)
    process_run = serializers.DictField(allow_null=True)
    starts_on = serializers.DateField()
    due_on = serializers.DateField(allow_null=True)
    overdue = serializers.BooleanField()
    form = serializers.DictField(allow_null=True)
    submission = SubmissionSerializer(allow_null=True)
    note = serializers.CharField()
    decision_note = serializers.CharField()
    submitted_by_name = serializers.CharField(allow_null=True)
    submitted_at = serializers.DateTimeField(allow_null=True)
    decided_by_name = serializers.CharField(allow_null=True)
    decided_at = serializers.DateTimeField(allow_null=True)
    can_submit = serializers.BooleanField()
    can_approve = serializers.BooleanField()
    can_cancel = serializers.BooleanField()
    is_test = serializers.BooleanField()
    created_at = serializers.DateTimeField()


class BuilderTaskCreateSerializer(serializers.Serializer):
    template_id = serializers.IntegerField(min_value=1, max_value=MAX_ID)
    household_id = serializers.IntegerField(min_value=1, max_value=MAX_ID, required=False, allow_null=True,
                                            default=None)
    assigned_to_id = serializers.IntegerField(min_value=1, max_value=MAX_ID, required=False, allow_null=True,
                                              default=None)
    starts_on = serializers.DateField(required=False, allow_null=True, default=None)
    due_on = serializers.DateField(required=False, allow_null=True, default=None)
    note = serializers.CharField(max_length=1000, required=False, allow_blank=True, default="")


class TaskSubmitSerializer(serializers.Serializer):
    data = serializers.JSONField(required=False, default=dict)


class TaskNoteSerializer(serializers.Serializer):
    note = serializers.CharField(max_length=1000, required=False, allow_blank=True, default="")


class TaskReturnSerializer(serializers.Serializer):
    note = serializers.CharField(max_length=1000)


class TaskCountsSerializer(serializers.Serializer):
    mine_open = serializers.IntegerField()
    to_approve = serializers.IntegerField()


# --- Process runs (§3.3) -------------------------------------------------------------------------


def run_payload(run: ProcessRun, tasks: list[TaskInstance]) -> dict[str, Any]:
    by_stage: dict[int, TaskInstance] = {}
    for task in sorted(tasks, key=lambda t: t.pk):
        if task.stage_index is not None:
            by_stage[task.stage_index] = task
    stages = []
    for index, stage in enumerate(run.stages or []):
        if run.status == "DONE" or index < run.current_index:
            state = "done"
        elif run.status == "CANCELLED":
            state = "cancelled"
        elif index == run.current_index:
            state = "current"
        else:
            state = "pending"
        task = by_stage.get(index)
        stages.append({
            "index": index,
            "title": stage.get("title", ""),
            "state": state,
            "task": {"id": task.pk, "code": task.code, "status": task.status,
                     "status_display": task.get_status_display()} if task is not None else None,
        })
    dt = serializers.DateTimeField()
    return {
        "id": run.pk,
        "code": run.code,
        "process_id": run.process_id,
        "process_name": run.process.name,
        "process_version": run.process_version,
        "household": household_ref(run.household),
        "status": run.status,
        "status_display": run.get_status_display(),
        "current_index": run.current_index,
        "is_test": run.is_test,
        "stages": stages,
        "cancel_reason": run.cancel_reason,
        "started_by_name": _name(run.started_by),
        "started_at": dt.to_representation(run.started_at),
        "finished_at": dt.to_representation(run.finished_at) if run.finished_at else None,
    }


def runs_payload(rows: list[ProcessRun]) -> list[dict]:
    tasks: dict[int, list[TaskInstance]] = {run.pk: [] for run in rows}
    for task in TaskInstance.objects.filter(process_run_id__in=list(tasks)).only(
        "id", "status", "stage_index", "process_run_id"
    ):
        tasks[task.process_run_id].append(task)
    return [run_payload(run, tasks[run.pk]) for run in rows]


class ProcessRunSerializer(serializers.Serializer):
    """Documentation shape of ``ProcessRun`` (built by :func:`run_payload`)."""

    id = serializers.IntegerField()
    code = serializers.CharField()
    process_id = serializers.IntegerField()
    process_name = serializers.CharField()
    process_version = serializers.IntegerField()
    household = _HouseholdRef(allow_null=True)
    status = serializers.CharField()
    status_display = serializers.CharField()
    current_index = serializers.IntegerField()
    is_test = serializers.BooleanField()
    stages = serializers.ListField(child=serializers.DictField())
    cancel_reason = serializers.CharField()
    started_by_name = serializers.CharField(allow_null=True)
    started_at = serializers.DateTimeField()
    finished_at = serializers.DateTimeField(allow_null=True)


class ProcessStartSerializer(serializers.Serializer):
    process_id = serializers.IntegerField(min_value=1, max_value=MAX_ID)
    household_id = serializers.IntegerField(min_value=1, max_value=MAX_ID, required=False, allow_null=True,
                                            default=None)


class ProcessCancelSerializer(serializers.Serializer):
    reason = serializers.CharField(max_length=500, required=False, allow_blank=True, default="")


# --- Meta (§1.3) ---------------------------------------------------------------------------------


class MetaSerializer(serializers.Serializer):
    kinds = serializers.ListField(child=serializers.DictField())
    statuses = serializers.ListField(child=serializers.DictField())
    field_types = serializers.ListField(child=serializers.DictField())
    modules = serializers.ListField(child=serializers.DictField())
    actions = serializers.ListField(child=serializers.DictField())
    system_roles = serializers.ListField(child=serializers.DictField())
    role_refs = serializers.ListField(child=serializers.DictField())
    placeholders = serializers.ListField(child=serializers.DictField())
    districts = serializers.ListField(child=serializers.CharField())

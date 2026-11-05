"""Audit snapshots of the Konstruktor objects (``op_id`` = the code: ``VZ-000005``, ``JR-000002``, ``FM-000001``)."""

from __future__ import annotations

from typing import Any

from audit import services as audit
from audit.models import AuditObjectType

from .models import Blueprint, FormSubmission, ProcessRun, TaskInstance, UserAccess


def _blueprint(bp: Blueprint) -> dict[str, Any]:
    return {
        "kind": bp.kind,
        "name": bp.name,
        "description": bp.description,
        "status": bp.status,
        "version": bp.version,
        "data": bp.data,
        "pending": bp.pending,
    }


def _task(task: TaskInstance) -> dict[str, Any]:
    return {
        "code": task.code,
        "template_id": task.template_id,
        "template_version": task.template_version,
        "title": task.title,
        "executor": task.executor,
        "approver": task.approver,
        "assigned_to_id": task.assigned_to_id,
        "household_id": task.household_id,
        "process_run_id": task.process_run_id,
        "stage_index": task.stage_index,
        "starts_on": task.starts_on,
        "due_on": task.due_on,
        "status": task.status,
        "decision_note": task.decision_note,
        "is_test": task.is_test,
    }


def _run(run: ProcessRun) -> dict[str, Any]:
    return {
        "code": run.code,
        "process_id": run.process_id,
        "process_version": run.process_version,
        "household_id": run.household_id,
        "current_index": run.current_index,
        "status": run.status,
        "is_test": run.is_test,
        "cancel_reason": run.cancel_reason,
    }


def _submission(sub: FormSubmission) -> dict[str, Any]:
    return {
        "code": sub.code,
        "form_id": sub.form_id,
        "form_version": sub.form_version,
        "household_id": sub.household_id,
        "task_id": sub.task_id,
        "data": sub.data,
        "is_test": sub.is_test,
    }


def _access(access: UserAccess) -> dict[str, Any]:
    return {
        "user_id": access.user_id,
        "profile_id": access.profile_id,
        "districts": list(access.districts or []),
        "household_ids": sorted(access.households.values_list("pk", flat=True)) if access.pk else [],
    }


def register() -> None:
    audit.register_snapshot(Blueprint, AuditObjectType.BLUEPRINT, _blueprint)
    audit.register_snapshot(TaskInstance, AuditObjectType.TASK, _task, op_id_attr="code")
    audit.register_snapshot(ProcessRun, AuditObjectType.PROCESS_RUN, _run, op_id_attr="code")
    audit.register_snapshot(FormSubmission, AuditObjectType.FORM_SUBMISSION, _submission, op_id_attr="code")
    audit.register_snapshot(UserAccess, AuditObjectType.USER_ACCESS, _access)

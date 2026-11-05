"""Konstruktor notifications (API_CONTRACT_V4 §3.2, §3.3). Queued after commit via ``notify_on_commit``.

Role references (``"VET"``, ``"profile:7"``) are resolved to concrete users here and passed as the contextual
``responsible`` recipients. Rows of test-mode objects (``is_test``) notify nobody.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from common.formatting import fmt_date
from notifications import registry
from notifications.compose import compose, household_rows
from notifications.services import notify_on_commit

if TYPE_CHECKING:
    from .models import ProcessRun, TaskInstance


def _task_path(task: TaskInstance) -> dict[str, str]:
    return {"staff_path": f"/tasks/{task.pk}", "owner_path": f"/tasks/{task.pk}"}


def _placeholders(task: TaskInstance, role_ref: str) -> dict[str, str]:
    from .services import ref_display

    return {
        "title": task.title,
        "code": task.code,
        "household": str(task.household) if task.household_id else "",
        "due": fmt_date(task.due_on) if task.due_on else "",
        "role": ref_display(role_ref),
    }


def _task_text(event: str, task: TaskInstance, status: str, extra: list | None = None) -> str:
    rows: list = [("Vazifa", task.title), ("Raqam", task.code)]
    if task.household_id:
        rows.extend(household_rows(task.household))
    if task.due_on:
        rows.append(("Muddat", fmt_date(task.due_on)))
    rows.extend(extra or [])
    return compose(event, rows, status, task.code)


def _executors(task: TaskInstance) -> list:
    from .services import users_for_refs

    if task.assigned_to_id:
        return [task.assigned_to]
    return users_for_refs([task.executor], task.household)


def task_assigned(task: TaskInstance) -> None:
    if task.is_test:
        return
    contextual = {registry.RESPONSIBLE: _executors(task)}
    if task.household_id:
        contextual[registry.HOUSEHOLD_OWNER] = task.household.owner
    notify_on_commit(
        "task_assigned",
        key=task.pk,
        text=_task_text("task_assigned", task, "Yangi"),
        related_op=task.code,
        contextual=contextual,
        object_type="TaskInstance",
        object_id=task.pk,
        placeholders=_placeholders(task, task.executor),
        **_task_path(task),
    )


def task_submitted(task: TaskInstance) -> None:
    from .services import users_for_refs

    if task.is_test or not task.approver:
        return
    notify_on_commit(
        "task_submitted",
        key=f"{task.pk}:{int(task.submitted_at.timestamp())}",
        text=_task_text("task_submitted", task, "Tasdiq kutilmoqda",
                        [("Topshirdi", task.submitted_by.full_name if task.submitted_by_id else "")]),
        related_op=task.code,
        contextual={registry.RESPONSIBLE: users_for_refs([task.approver], task.household)},
        object_type="TaskInstance",
        object_id=task.pk,
        placeholders=_placeholders(task, task.approver),
        **_task_path(task),
    )


def _decided(event: str, task: TaskInstance, status: str) -> None:
    if task.is_test:
        return
    recipients = [task.submitted_by] if task.submitted_by_id else _executors(task)
    notify_on_commit(
        event,
        key=f"{task.pk}:{int(task.decided_at.timestamp())}",
        text=_task_text(event, task, status, [("Izoh", task.decision_note)]),
        related_op=task.code,
        contextual={registry.RESPONSIBLE: recipients},
        object_type="TaskInstance",
        object_id=task.pk,
        placeholders=_placeholders(task, task.executor),
        **_task_path(task),
    )


def task_approved(task: TaskInstance) -> None:
    _decided("task_approved", task, "Tasdiqlandi")


def task_returned(task: TaskInstance) -> None:
    _decided("task_returned", task, "Qaytarildi")


def _run_rows(run: ProcessRun, index: int | None = None) -> list:
    rows: list = [("Jarayon", run.process.name), ("Raqam", run.code)]
    if index is not None and index < len(run.stages):
        rows.append(("Bosqich", f"{index + 1}. {run.stages[index].get('title', '')}"))
    if run.household_id:
        rows.extend(household_rows(run.household))
    return rows


def _run_kwargs(run: ProcessRun) -> dict:
    return {
        "related_op": run.code,
        "staff_path": f"/processes/{run.pk}",
        "owner_path": "/my",
        "object_type": "ProcessRun",
        "object_id": run.pk,
        "placeholders": {"title": run.process.name, "code": run.code,
                         "household": str(run.household) if run.household_id else ""},
    }


def stage_started(run: ProcessRun, index: int) -> None:
    from .services import users_for_refs

    if run.is_test:
        return
    roles = (run.stages[index] or {}).get("notify_roles") or []
    if not roles:
        return
    notify_on_commit(
        "process_stage_started",
        key=f"{run.pk}:{index}",
        text=compose("process_stage_started", _run_rows(run, index), "Boshlandi", run.code),
        contextual={registry.RESPONSIBLE: users_for_refs(roles, run.household)},
        **_run_kwargs(run),
    )


def process_finished(run: ProcessRun) -> None:
    if run.is_test:
        return
    notify_on_commit(
        "process_finished",
        key=run.pk,
        text=compose("process_finished", _run_rows(run), "Yakunlandi", run.code),
        **_run_kwargs(run),
    )


def process_blocked(run: ProcessRun, index: int) -> None:
    if run.is_test:
        return
    notify_on_commit(
        "process_blocked",
        key=f"{run.pk}:{index}",
        text=compose("process_blocked", _run_rows(run, index), "To‘xtab qoldi", run.code),
        **_run_kwargs(run),
    )

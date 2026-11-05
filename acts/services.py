"""Dalolatnomalar (API_CONTRACT_V4 §6): access rules, template selection / rendering and frozen snapshots."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date
from typing import Any

from django.db import transaction
from django.db.models import Q, QuerySet
from django.utils import timezone
from rest_framework.exceptions import APIException, NotFound, PermissionDenied, ValidationError

from accounts.models import User
from audit import services as audit
from audit.models import AuditAction, AuditModule
from builder.access import matches_ref, scope_households
from builder.models import Blueprint, BlueprintKind, BlueprintStatus, TaskInstance, TaskStatus
from common.formatting import fmt_date
from common.roles import Role, is_super_admin, role_of
from farm.models import Household

from . import content
from .content import DASH, ROLE_ORDER, RoleContent, Window, fmt_moment
from .models import Act, ActKind

MAX_PERIOD_DAYS = 366
SIGN_LINE = "____________"
DIRECTOR = "Direktor"

MSG_NO_ACCESS = "Bu dalolatnoma uchun ruxsat yo‘q."
MSG_HOUSEHOLD_NOT_FOUND = "Xonadon topilmadi."
MSG_USER_NOT_FOUND = "Xodim topilmadi."
MSG_TASK_NOT_FOUND = "Vazifa topilmadi."
MSG_TASK_NOT_APPROVED = "Dalolatnoma faqat tasdiqlangan vazifa uchun tuziladi."

#: "Barcha veterinarlar" … (``responsible`` of a role act without a user).
ALL_OF_ROLE = {
    Role.ADMIN: "Barcha administratorlar",
    Role.WAREHOUSE: "Barcha omborchilar",
    Role.LOGISTICS: "Barcha logistika xodimlari",
    Role.VET: "Barcha veterinarlar",
    Role.OWNER: "Barcha xonadon egalari",
}
GENERAL_TITLE = "Umumiy dalolatnoma"
OWNER_LABEL = Role.OWNER.label  # "Xonadon egasi"


class Conflict(APIException):
    status_code = 409
    default_detail = MSG_TASK_NOT_APPROVED
    default_code = "conflict"


@dataclass
class ActRequest:
    """A validated, access-checked act request (shared by preview and create)."""

    kind: str
    actor: User
    role: str = ""
    user: User | None = None
    household: Household | None = None
    task: TaskInstance | None = None
    date_from: date | None = None
    date_to: date | None = None


# --- Access ------------------------------------------------------------------------------------


def _visible_household(actor: User, household_id: int) -> Household:
    household = scope_households(actor, Household.objects.select_related("owner")).filter(pk=household_id).first()
    if household is None:
        raise NotFound(MSG_HOUSEHOLD_NOT_FOUND)
    return household


def can_see_task(actor: User, task: TaskInstance) -> bool:
    """V4 §3.2 task visibility: executor, approver, ADMIN, OWNER of its household (test tasks: Super Admin)."""
    if task.is_test and not is_super_admin(actor):
        return False
    role = role_of(actor)
    if task.household_id is not None and not scope_households(
        actor, Household.objects.filter(pk=task.household_id)
    ).exists():
        return False
    if role == Role.ADMIN:
        return True
    if role == Role.OWNER:
        return task.household is not None and task.household.owner_id == actor.pk
    return (
        task.assigned_to_id == actor.pk
        or task.submitted_by_id == actor.pk
        or matches_ref(actor, task.executor)
        or matches_ref(actor, task.approver)
    )


def _period(date_from: date | None, date_to: date | None) -> tuple[date, date]:
    today = timezone.localdate()
    date_to = date_to or today
    date_from = date_from or date_to.replace(day=1)
    if date_from > date_to:
        raise ValidationError({"date_from": ["Boshlanish sanasi tugash sanasidan keyin bo‘lmasligi kerak."]})
    if (date_to - date_from).days + 1 > MAX_PERIOD_DAYS:
        raise ValidationError({"date_to": [f"Davr {MAX_PERIOD_DAYS} kundan oshmasligi kerak."]})
    return date_from, date_to


def resolve(actor: User, data: dict[str, Any]) -> ActRequest:
    """Apply the §6 access rules to ``{kind, role?, user_id?, household_id?, task_id?, date_from?, date_to?}``."""
    kind = data["kind"]
    actor_role = role_of(actor)
    if kind == ActKind.TASK:
        task = (
            TaskInstance.objects.select_related("household__owner", "assigned_to", "submitted_by", "decided_by")
            .filter(pk=data.get("task_id"))
            .first()
        )
        if task is None or not can_see_task(actor, task):
            raise NotFound(MSG_TASK_NOT_FOUND)
        if task.status != TaskStatus.APPROVED:
            raise Conflict(MSG_TASK_NOT_APPROVED)
        return ActRequest(kind=kind, actor=actor, task=task, household=task.household)

    date_from, date_to = _period(data.get("date_from"), data.get("date_to"))
    request = ActRequest(kind=kind, actor=actor, date_from=date_from, date_to=date_to)
    household_id = data.get("household_id")
    user_id = data.get("user_id")

    if actor_role == Role.OWNER:
        if kind == ActKind.ROLE and data.get("role") != Role.OWNER:
            raise PermissionDenied(MSG_NO_ACCESS)
        own = Household.objects.select_related("owner").filter(owner=actor).first()
        if own is None or (household_id is not None and household_id != own.pk):
            raise PermissionDenied(MSG_NO_ACCESS) if own is None else NotFound(MSG_HOUSEHOLD_NOT_FOUND)
        if user_id is not None and user_id != actor.pk:
            raise PermissionDenied(MSG_NO_ACCESS)
        request.household = own
        if kind == ActKind.ROLE:
            request.role, request.user = Role.OWNER, actor
        return request

    if actor_role != Role.ADMIN:
        # WAREHOUSE / LOGISTICS / VET: only the role act of their own role, about their own work.
        if kind != ActKind.ROLE or data.get("role") != actor_role:
            raise PermissionDenied(MSG_NO_ACCESS)
        if user_id is not None and user_id != actor.pk:
            raise PermissionDenied(MSG_NO_ACCESS)
        request.role, request.user = actor_role, actor
    elif kind == ActKind.ROLE:
        request.role = data.get("role") or ""
        if not request.role:
            raise ValidationError({"role": ["Bu maydon to‘ldirilishi shart."]})
        if user_id is not None:
            user = User.objects.filter(pk=user_id, deleted_at__isnull=True).first()
            if user is None:
                raise ValidationError({"user_id": [MSG_USER_NOT_FOUND]})
            if role_of(user) != request.role:
                raise ValidationError({"user_id": ["Xodimning roli tanlangan rolga mos emas."]})
            request.user = user
    if household_id is not None:
        request.household = _visible_household(actor, household_id)
    if request.role == Role.OWNER and request.user is not None:
        own = Household.objects.select_related("owner").filter(owner=request.user).first()
        if own is None or (request.household is not None and request.household.pk != own.pk):
            raise ValidationError({"household_id": ["Xonadon tanlangan xonadon egasiga tegishli emas."]})
        request.household = _visible_household(actor, own.pk)
    return request


def visible_acts(user: User) -> QuerySet[Act]:
    """Acts the caller may list / open / download (V4 §6 access + §2 scope)."""
    qs = Act.objects.select_related("user", "household", "created_by")
    role = role_of(user)
    if role == Role.ADMIN:
        return scope_households(user, qs, "household")
    if role == Role.OWNER:
        return qs.filter(household__owner=user).filter(
            Q(kind=ActKind.ROLE, role=Role.OWNER) | Q(kind=ActKind.GENERAL) | Q(kind=ActKind.TASK)
        )
    if role in (Role.WAREHOUSE, Role.LOGISTICS, Role.VET):
        own = Q(kind=ActKind.ROLE, role=role, user=user) | Q(kind=ActKind.TASK, created_by=user)
        return scope_households(user, qs.filter(own), "household")
    return qs.none()


# --- Templates ---------------------------------------------------------------------------------

PLACEHOLDER = re.compile(r"\{\{\s*([A-Za-z0-9_.]+)\s*\}\}")


def render(body: str, values: dict[str, str]) -> str:
    """``{{key}}`` → ``values[key]``; unknown placeholders stay as they are."""
    return PLACEHOLDER.sub(lambda match: values.get(match.group(1), match.group(0)), body or "")


def select_template(scope: str, role: str = "") -> Blueprint | None:
    """Active document blueprint of ``scope`` (and ``role``) with the highest id."""
    candidates = Blueprint.objects.filter(kind=BlueprintKind.DOCUMENT, status=BlueprintStatus.ACTIVE).order_by("-pk")
    for blueprint in candidates:
        data = blueprint.data if isinstance(blueprint.data, dict) else {}
        if data.get("scope") == scope and (scope != "role" or data.get("role") == role):
            return blueprint
    return None


def task_template(task: TaskInstance) -> Blueprint | None:
    """The document of the task definition (``document_id``), or ``None`` for the default layout."""
    document_id = (task.definition or {}).get("document_id") if isinstance(task.definition, dict) else None
    if not isinstance(document_id, int) or isinstance(document_id, bool):
        return None
    return Blueprint.objects.filter(pk=document_id, kind=BlueprintKind.DOCUMENT).first()


def _template_data(template: Blueprint | None) -> dict[str, Any]:
    data = template.data if template is not None and isinstance(template.data, dict) else {}
    return {
        "title": str(data.get("title") or ""),
        "body": str(data.get("body") or ""),
        "signatures": [str(item) for item in data.get("signatures") or [] if str(item).strip()],
        "include_photos": data.get("include_photos", True) is not False,
        "include_approvals": data.get("include_approvals", True) is not False,
        "include_quantities": data.get("include_quantities", True) is not False,
    }


# --- Snapshot ----------------------------------------------------------------------------------


def _household_block(household: Household | None) -> dict[str, str] | None:
    if household is None:
        return None
    return {
        "code": household.code,
        "name": household.name,
        "owner": household.owner.full_name,
        "district": household.district,
        "address": ", ".join(part for part in (household.region, household.district, household.mfy, household.address) if part),
    }


def _company() -> str:
    from core.settings_service import get_settings

    return get_settings().company_name


def _signature(label: str, name: str = "") -> str:
    return f"{label}: {SIGN_LINE} {name}".rstrip()


def _signatures(labels: list[str], named: dict[str, str]) -> list[str]:
    """Template signature labels → lines; a label matching a known signer gets the name."""
    lowered = {key.lower(): value for key, value in named.items()}
    return [_signature(label, lowered.get(label.strip().lower(), "")) for label in labels]


def _blocks(summary: list[dict], photos: list[dict], approvals: list[dict]) -> dict[str, str]:
    return {
        "quantities": "\n".join(f"{item['label']}: {item['value']}" for item in summary),
        "photos": f"{len(photos)} ta rasm ilova qilingan" if photos else "",
        "approvals": "\n".join(f"{item['label']}: {item['name']} ({item['at']})" for item in approvals),
    }


def _role_content(role: str, window: Window) -> tuple[RoleContent, list[TaskInstance]]:
    return content.BUILDERS[role](window), content.approved_tasks(window, role)


def build_snapshot(request: ActRequest, code: str = "") -> tuple[dict[str, Any], Blueprint | None]:
    """The act snapshot (``ActSnapshot`` of the frontend) and the template it used."""
    if request.kind == ActKind.TASK:
        return _task_snapshot(request, code)
    window = Window(request.date_from, request.date_to, household=request.household, user=request.user,
                    viewer=request.actor)
    household = _household_block(request.household)
    period = f"{fmt_date(request.date_from)} — {fmt_date(request.date_to)}"
    owner_name = household["owner"] if household else ""

    if request.kind == ActKind.ROLE:
        role = Role(request.role)
        template = select_template("role", role)
        role_content, tasks = _role_content(role, window)
        summary = [{"label": label, "value": value} for label, value in role_content.summary]
        summary.append({"label": "Tasdiqlangan vazifalar", "value": content.text(len(tasks))})
        sections = [section.as_dict() for section in role_content.sections]
        task_items, photos = content.tasks_block(tasks)
        if request.user is not None:
            responsible = request.user.full_name
        elif role == Role.OWNER and request.household is not None:
            responsible = owner_name
        else:
            responsible = ALL_OF_ROLE[role]
        title = f"{role.label} bajargan ishlar dalolatnomasi"
        default_body = f"Ushbu dalolatnoma {period} davrida {responsible} tomonidan bajarilgan ishlarni tasdiqlaydi."
        signer_name = request.user.full_name if request.user is not None else ""
        named = {role.label: signer_name, OWNER_LABEL: owner_name}
        default_signatures = [_signature(role.label, signer_name)]
        if household and role != Role.OWNER:
            default_signatures.append(_signature(OWNER_LABEL, owner_name))
        role_value, role_display = role.value, role.label
    else:
        template = select_template("general")
        summary, sections, tasks = [], [], []
        total = 0
        for role in ROLE_ORDER:
            role_content, role_tasks = _role_content(role, window)
            sections.append(
                {
                    "title": f"{role.label}: yakuniy ko‘rsatkichlar",
                    "columns": ["Ko‘rsatkich", "Qiymat"],
                    "rows": [[label, value] for label, value in role_content.summary]
                    + [["Tasdiqlangan vazifalar", content.text(len(role_tasks))]],
                }
            )
            sections.extend(
                {**section.as_dict(), "title": f"{role.label}: {section.title}"} for section in role_content.sections
            )
            count = content.counted(role_content, len(role_tasks))
            total += count
            summary.append({"label": role.label, "value": f"{content.text(count)} ta ish"})
            tasks.extend(task for task in role_tasks if task.pk not in {t.pk for t in tasks})
        summary.append({"label": "Jami bajarilgan ishlar", "value": f"{content.text(total)} ta ish"})
        task_items, photos = content.tasks_block(tasks)
        responsible = "Barcha rollar"
        title = GENERAL_TITLE
        default_body = (
            f"Ushbu umumiy dalolatnoma {period} davrida barcha rollar tomonidan bajarilgan ishlarni jamlaydi."
        )
        named = {OWNER_LABEL: owner_name}
        default_signatures = [_signature(role.label, owner_name if role == Role.OWNER else "") for role in ROLE_ORDER]
        default_signatures.append(_signature(DIRECTOR))
        role_value, role_display = None, None

    if household and request.kind == ActKind.ROLE and not template:
        default_body += f" Xonadon: {household['code']} {household['name']}."
    settings = _template_data(template)
    if not settings["include_quantities"]:
        summary = []
    if not settings["include_photos"]:
        photos = []
    snapshot = {
        "code": code,
        "title": settings["title"] or title,
        "kind": request.kind,
        "role": role_value,
        "role_display": role_display,
        "company": _company(),
        "date": fmt_date(timezone.localdate()),
        "period": period,
        "household": household,
        "responsible": responsible,
        "body": "",
        "summary": summary,
        "sections": sections,
        "tasks": task_items,
        "photos": photos,
        "approvals": [],
        "signatures": _signatures(settings["signatures"], named) if settings["signatures"] else default_signatures,
    }
    if request.kind == ActKind.GENERAL and settings["signatures"] and DIRECTOR not in settings["signatures"]:
        snapshot["signatures"].append(_signature(DIRECTOR))
    snapshot["body"] = render(settings["body"], _values(snapshot)) if settings["body"] else default_body
    return snapshot, template


def _task_snapshot(request: ActRequest, code: str) -> tuple[dict[str, Any], Blueprint | None]:
    task = request.task
    template = task_template(task)
    settings = _template_data(template)
    submission = content.latest_submissions([task]).get(task.pk)
    names = content.file_names_of([submission] if submission is not None else [])
    values, photos, by_key = content.submission_values(submission, names)
    household = _household_block(task.household)
    executor = task.submitted_by or task.assigned_to
    approvals = []
    if task.submitted_by_id:
        approvals.append({"label": "Bajardi", "name": task.submitted_by.full_name, "at": fmt_moment(task.submitted_at)})
    if task.decided_by_id:
        approvals.append({"label": "Tasdiqladi", "name": task.decided_by.full_name, "at": fmt_moment(task.decided_at)})
    decided_on = timezone.localtime(task.decided_at).date() if task.decided_at else task.starts_on
    period = f"{fmt_date(task.starts_on)} — {fmt_date(decided_on)}" if task.starts_on < decided_on else fmt_date(decided_on)
    executor_label = _ref_label(task.executor)
    approver_label = _ref_label(task.approver) if task.approver else ""
    summary = [
        {"label": "Vazifa", "value": f"{task.code} {task.title}"},
        {"label": "Bajaruvchi", "value": executor.full_name if executor else executor_label},
        {"label": "Tasdiqlangan", "value": fmt_moment(task.decided_at)},
    ]
    if not settings["include_quantities"]:
        summary = []
    named = {
        executor_label: executor.full_name if executor else "",
        OWNER_LABEL: household["owner"] if household else "",
    }
    if approver_label:
        named.setdefault(approver_label, task.decided_by.full_name if task.decided_by else "")
    if settings["signatures"]:
        signatures = _signatures(settings["signatures"], named)
    else:
        signatures = [_signature(executor_label, named[executor_label])]
        if approver_label and approver_label != executor_label:
            signatures.append(_signature(approver_label, named[approver_label]))
        if household:
            signatures.append(_signature(OWNER_LABEL, household["owner"]))
    snapshot = {
        "code": code,
        "title": settings["title"] or f"{task.title}: bajarilgan ish dalolatnomasi",
        "kind": ActKind.TASK.value,
        "role": None,
        "role_display": None,
        "company": _company(),
        "date": fmt_date(timezone.localdate()),
        "period": period,
        "household": household,
        "responsible": executor.full_name if executor else executor_label,
        "body": "",
        "summary": summary,
        "sections": [
            {"title": "Vazifa natijalari", "columns": ["Maydon", "Qiymat"],
             "rows": [[item["label"], item["value"]] for item in values]}
        ] if values else [],
        "tasks": [
            {"code": task.code, "title": task.title, "household": content.household_label(task.household),
             "approved_at": fmt_moment(task.decided_at), "values": values}
        ],
        "photos": photos if settings["include_photos"] else [],
        "approvals": approvals if settings["include_approvals"] else [],
        "signatures": signatures,
    }
    default_body = (
        f"Ushbu dalolatnoma {task.code} “{task.title}” vazifasi bajarilib, {fmt_date(decided_on)} kuni "
        "tasdiqlanganini qayd etadi."
    )
    extra = {"task.code": task.code, "task.title": task.title, **{f"field.{k}": v for k, v in by_key.items()}}
    snapshot["body"] = render(settings["body"], _values(snapshot, extra)) if settings["body"] else default_body
    return snapshot, template


def _ref_label(ref: str) -> str:
    if ref in Role.values:
        return Role(ref).label
    if ref.startswith("profile:"):
        tail = ref.split(":", 1)[1]
        profile = Blueprint.objects.filter(pk=int(tail)).only("name").first() if tail.isdigit() else None
        if profile is not None:
            return profile.name
    return ref or DASH


def _values(snapshot: dict[str, Any], extra: dict[str, str] | None = None) -> dict[str, str]:
    household = snapshot["household"] or {}
    values = {
        "company": snapshot["company"],
        "date": snapshot["date"],
        "period": snapshot["period"],
        "act.code": snapshot["code"],
        "role": snapshot["role_display"] or "",
        "responsible": snapshot["responsible"],
        **{f"household.{key}": household.get(key, "") for key in ("code", "name", "owner", "district", "address")},
        **_blocks(snapshot["summary"], snapshot["photos"], snapshot["approvals"]),
    }
    values.update(extra or {})
    return values


# --- Create ------------------------------------------------------------------------------------


def create_act(request: ActRequest) -> Act:
    """Insert the act, freeze its snapshot (with its ``DL-`` code) and audit ``acts / create``."""
    with transaction.atomic():
        act = Act.objects.create(
            kind=request.kind,
            role=request.role or "",
            user=request.user,
            household=request.household,
            task=request.task,
            date_from=request.date_from,
            date_to=request.date_to,
            created_by=request.actor,
        )
        snapshot, template = build_snapshot(request, act.code)
        act.snapshot = snapshot
        act.template = template
        act.template_version = template.version if template is not None else None
        act.save(update_fields=["snapshot", "template", "template_version"])
        audit.record_create(AuditModule.ACTS, act, request.actor)
    return act


def record_export(act: Act, actor: User) -> None:
    audit.record(
        module=AuditModule.ACTS,
        action=AuditAction.EXPORT,
        obj=act,
        actor=actor,
        after={"code": act.code, "format": "pdf"},
    )


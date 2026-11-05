"""Business logic of the Konstruktor (API_CONTRACT_V4 §1–§3).

Every state change: ``transaction.atomic()`` → ``select_for_update()`` (lock order: ProcessRun → TaskInstance →
Blueprint → User/UserAccess) → validate the CURRENT status (``409`` otherwise) → change rows →
``audit.services.record*`` (same transaction) → ``builder.events.*`` (queued after commit only).
"""

from __future__ import annotations

from collections import defaultdict
from datetime import date
from typing import Any

from django.db import transaction
from django.db.models import Count, Max, Q, QuerySet
from django.utils import timezone
from rest_framework import status
from rest_framework.exceptions import APIException, NotFound, PermissionDenied, ValidationError

from accounts import tokens
from accounts.models import User
from audit import services as audit
from audit.models import AuditAction, AuditModule
from common.permissions import MSG_NO_PERMISSION
from common.roles import ALL_ROLES, Role, is_super_admin, role_of
from core import files
from core.models import AttachmentPurpose
from farm.models import Household

from . import events, schemas
from .access import PROFILE_PREFIX, access_of, matches_any, matches_ref, profile_id_of, scope_households
from .models import (
    Blueprint,
    BlueprintKind,
    BlueprintStatus,
    ConfigVersion,
    FormSubmission,
    ProcessRun,
    ProcessRunStatus,
    TaskInstance,
    TaskStatus,
    UserAccess,
)

BLUEPRINT_OBJECT = "blueprint"

MSG_NO_CHANGE = "O‘zgarish yo‘q"
MSG_ARCHIVED = "Arxivlangan obyektni o‘zgartirib bo‘lmaydi. Avval uni tiklang."
MSG_TEST_DRAFT_ONLY = "Sinov rejimiga faqat qoralamani o‘tkazish mumkin."
MSG_ALREADY_ARCHIVED = "Obyekt allaqachon arxivlangan."
MSG_RESTORE_ARCHIVED_ONLY = "Faqat arxivlangan obyektni tiklash mumkin."
MSG_DELETE_DRAFT_ONLY = "Faqat qoralamani o‘chirish mumkin."
MSG_DELETE_REFERENCED = "Obyekt ishlatilgan yoki unga bog‘langan obyektlar bor: o‘chirib bo‘lmaydi, arxivlang."
MSG_VERSION_NOT_FOUND = "Bunday versiya topilmadi."
MSG_PROFILE_ACTIVE_ONLY = "Faqat faol rol profilini tanlash mumkin."
MSG_PROFILE_SUPERUSER = "Super Admin uchun rol profili belgilanmaydi."
MSG_PROFILE_OWNER = "Xonadon egasiga rol profili belgilanmaydi."
MSG_HOUSEHOLD_NOT_FOUND = "Xonadon topilmadi."
MSG_HOUSEHOLD_INACTIVE = "Xonadon faol emas"
MSG_REQUIRED = "Bu maydon to‘ldirilishi shart."
MSG_FORM_NOT_FOUND = "Forma topilmadi yoki faol emas."
MSG_FORM_NOT_ALLOWED = "Bu formani to‘ldirish uchun ruxsat yo‘q."
MSG_TEMPLATE_NOT_FOUND = "Vazifa shabloni topilmadi yoki faol emas."
MSG_PROCESS_NOT_FOUND = "Jarayon topilmadi yoki faol emas."
MSG_ASSIGNEE = "Tanlangan xodim vazifa bajaruvchisi roliga mos emas."
MSG_DUE_BEFORE_START = "Tugash sanasi boshlanish sanasidan oldin bo‘lishi mumkin emas."
MSG_NOT_EXECUTOR = "Bu vazifa sizga biriktirilmagan."
MSG_NOT_APPROVER = "Bu vazifani tasdiqlash huquqi sizda yo‘q."
MSG_NOTE_REQUIRED = "Qaytarish sababini yozing."
MSG_STAGE_UNAVAILABLE = "{index}-bosqich vazifa shabloni topilmadi yoki faol emas."


class Conflict(APIException):
    """``409`` — the object is not in a state that allows the action (stale page, references)."""

    status_code = status.HTTP_409_CONFLICT
    default_detail = "Amalni bajarib bo‘lmaydi."
    default_code = "conflict"


def _bad(message: str, field: str | None = None) -> ValidationError:
    return ValidationError({field: [message]} if field else {"detail": message})


# --- Config versions -----------------------------------------------------------------------------


def write_config_version(
    object_type: str,
    object_id: int,
    *,
    name: str = "",
    data: dict | None = None,
    actor: User | None = None,
    comment: str = "",
    status: str = "",
    version: int | None = None,
) -> ConfigVersion:
    """Append one history row (``version`` defaults to the next number of that object)."""
    if version is None:
        last = ConfigVersion.objects.filter(object_type=object_type, object_id=object_id).aggregate(v=Max("version"))
        version = (last["v"] or 0) + 1
    return ConfigVersion.objects.create(
        object_type=object_type,
        object_id=object_id,
        version=version,
        name=(name or "")[:200],
        data=audit.to_json(data or {}),
        status=status,
        comment=(comment or "")[:500],
        created_by=actor if actor is not None and actor.is_authenticated else None,
    )


def blueprint_versions(bp: Blueprint) -> QuerySet[ConfigVersion]:
    return ConfigVersion.objects.filter(object_type=BLUEPRINT_OBJECT, object_id=bp.pk).select_related(
        "created_by"
    ).order_by("-version")


# --- Blueprint lifecycle (§1) --------------------------------------------------------------------


def _state(bp: Blueprint) -> dict[str, Any]:
    return {"name": bp.name, "description": bp.description, "data": bp.data}


def _lock(pk: int) -> Blueprint:
    bp = Blueprint.objects.select_for_update().filter(pk=pk).first()
    if bp is None:
        raise NotFound("Topilmadi.")
    return bp


def _refs_status_check(kind: str, data: dict, allowed: set[str]) -> None:
    """Every blueprint referenced by ``data`` must have a status in ``allowed`` (400 otherwise)."""
    for target_kind, ids in schemas.refs_in(data, kind).items():
        if not ids:
            continue
        for row in Blueprint.objects.filter(pk__in=ids).exclude(status__in=allowed).order_by("id"):
            raise _bad(
                f"Bog‘langan obyekt “{row.name}” ({row.get_kind_display()}) holati: {row.get_status_display()}. "
                "Avval uni faollashtiring."
            )
        found = set(Blueprint.objects.filter(pk__in=ids).values_list("pk", flat=True))
        if found != ids:
            raise _bad("Bog‘langan obyekt topilmadi.")


def _sync_profile_users(bp: Blueprint, old_base: str | None, actor: User) -> None:
    """A role profile's base role changed: its users get the new role and lose their sessions (§2)."""
    new_base = (bp.data or {}).get("base_role")
    if bp.kind != BlueprintKind.ROLE or not new_base or new_base == old_base:
        return
    for user in User.objects.select_for_update().filter(access__profile=bp, is_superuser=False).order_by("id"):
        _set_role_and_revoke(user, new_base, actor)


def _set_role_and_revoke(user: User, role: str, actor: User) -> None:
    before = audit.snapshot(user)
    user.role = role
    tokens.bump_token_version(user)
    user.sessions_revoked_at = timezone.now()
    user.save(update_fields=["role", "token_version", "sessions_revoked_at"])
    audit.record_update(AuditModule.USERS, user, actor, before)
    audit.record(module=AuditModule.USERS, action=AuditAction.SESSIONS_REVOKED, obj=user, actor=actor,
                 after={"reason": "role_profile"})


def _apply(bp: Blueprint, state: dict[str, Any], actor: User, comment: str) -> None:
    """Write ``state`` in place: ``version += 1`` + history row + audit."""
    before = audit.snapshot(bp)
    old_base = (bp.data or {}).get("base_role")
    bp.name = state["name"]
    bp.description = state["description"]
    bp.data = state["data"]
    bp.version += 1
    bp.save()
    write_config_version(BLUEPRINT_OBJECT, bp.pk, name=bp.name, data=_state(bp), actor=actor, comment=comment,
                         status=bp.status, version=bp.version)
    audit.record_update(AuditModule.BUILDER, bp, actor, before)
    if bp.status == BlueprintStatus.ACTIVE:
        _sync_profile_users(bp, old_base, actor)


def create_blueprint(*, actor: User, kind: str, name: str, description: str = "", data: Any) -> Blueprint:
    data = schemas.validate_data(kind, data)
    with transaction.atomic():
        bp = Blueprint.objects.create(kind=kind, name=name, description=description, data=data, created_by=actor)
        write_config_version(BLUEPRINT_OBJECT, bp.pk, name=bp.name, data=_state(bp), actor=actor,
                             comment="Yaratildi", status=bp.status, version=1)
        audit.record_create(AuditModule.BUILDER, bp, actor)
    return bp


def _change(bp: Blueprint, changes: dict[str, Any], actor: User) -> Blueprint:
    """PATCH semantics on a locked row: in place for draft/test, parked in ``pending`` for active."""
    if bp.status == BlueprintStatus.ARCHIVED:
        raise Conflict(MSG_ARCHIVED)
    base = bp.pending if bp.status == BlueprintStatus.ACTIVE and bp.pending else _state(bp)
    state = {
        "name": changes.get("name", base.get("name", bp.name)),
        "description": changes.get("description", base.get("description", "")),
        "data": schemas.validate_data(bp.kind, changes["data"]) if "data" in changes else base.get("data", {}),
    }
    comment = changes.get("comment", "") or ""
    if bp.status == BlueprintStatus.ACTIVE:
        before = audit.snapshot(bp)
        bp.pending = {**state, "comment": comment}
        bp.pending_by = actor
        bp.pending_at = timezone.now()
        bp.save(update_fields=["pending", "pending_by", "pending_at", "updated_at"])
        audit.record_update(AuditModule.BUILDER, bp, actor, before)
    elif state != _state(bp):
        _apply(bp, state, actor, comment)
    return bp


def update_blueprint(*, actor: User, pk: int, changes: dict[str, Any]) -> Blueprint:
    with transaction.atomic():
        return _change(_lock(pk), changes, actor)


def _status_change(bp: Blueprint, new_status: str, actor: User, **extra: Any) -> None:
    old = bp.status
    bp.status = new_status
    for name, value in extra.items():
        setattr(bp, name, value)
    bp.save()
    audit.record(module=AuditModule.BUILDER, action=AuditAction.STATUS_CHANGE, obj=bp, actor=actor,
                 before={"status": old}, after={"status": new_status})


def test_blueprint(*, actor: User, pk: int) -> Blueprint:
    with transaction.atomic():
        bp = _lock(pk)
        if bp.status != BlueprintStatus.DRAFT:
            raise _bad(MSG_TEST_DRAFT_ONLY)
        bp.data = schemas.validate_data(bp.kind, bp.data)
        _refs_status_check(bp.kind, bp.data, {BlueprintStatus.TEST, BlueprintStatus.ACTIVE})
        _status_change(bp, BlueprintStatus.TEST, actor)
    return bp


def activate_blueprint(*, actor: User, pk: int, comment: str = "") -> Blueprint:
    """draft/test → active, or apply the pending change of an active blueprint ("Tasdiqlash")."""
    with transaction.atomic():
        bp = _lock(pk)
        now = timezone.now()
        if bp.status in (BlueprintStatus.DRAFT, BlueprintStatus.TEST):
            bp.data = schemas.validate_data(bp.kind, bp.data)
            _refs_status_check(bp.kind, bp.data, {BlueprintStatus.ACTIVE})
            _status_change(bp, BlueprintStatus.ACTIVE, actor, activated_by=actor, activated_at=now,
                           pending=None, pending_by=None, pending_at=None)
        elif bp.status == BlueprintStatus.ACTIVE and bp.pending:
            pending = bp.pending
            state = {
                "name": pending.get("name", bp.name),
                "description": pending.get("description", bp.description),
                "data": schemas.validate_data(bp.kind, pending.get("data", bp.data)),
            }
            _refs_status_check(bp.kind, state["data"], {BlueprintStatus.ACTIVE})
            bp.pending, bp.pending_by, bp.pending_at = None, None, None
            bp.activated_by, bp.activated_at = actor, now
            _apply(bp, state, actor, comment or pending.get("comment", "") or "Tasdiqlandi")
        else:
            raise _bad(MSG_NO_CHANGE)
    return bp


def discard_pending(*, actor: User, pk: int) -> Blueprint:
    with transaction.atomic():
        bp = _lock(pk)
        if not bp.pending:
            raise _bad(MSG_NO_CHANGE)
        before = audit.snapshot(bp)
        bp.pending, bp.pending_by, bp.pending_at = None, None, None
        bp.save(update_fields=["pending", "pending_by", "pending_at", "updated_at"])
        audit.record_update(AuditModule.BUILDER, bp, actor, before)
    return bp


def _referencing(bp: Blueprint, *, include_archived: bool) -> list[Blueprint]:
    """Blueprints whose ``data`` references ``bp``."""
    qs = Blueprint.objects.exclude(pk=bp.pk).filter(
        kind__in=[BlueprintKind.TASK, BlueprintKind.PROCESS, BlueprintKind.FORM]
    )
    if not include_archived:
        qs = qs.exclude(status=BlueprintStatus.ARCHIVED)
    return [row for row in qs.order_by("id") if bp.pk in schemas.refs_in(row.data, row.kind).get(bp.kind, set())]


def archive_blockers(bp: Blueprint) -> list[str]:
    """Why ``bp`` cannot be archived (§1 "Rules that protect data"); empty = allowed."""
    reasons: list[str] = []
    if bp.kind == BlueprintKind.ROLE:
        users = UserAccess.objects.filter(profile=bp).count()
        if users:
            reasons.append(f"Rol profili {users} ta foydalanuvchiga biriktirilgan.")
    elif bp.kind in (BlueprintKind.FORM, BlueprintKind.DOCUMENT, BlueprintKind.TASK):
        users_of = BlueprintKind.PROCESS if bp.kind == BlueprintKind.TASK else BlueprintKind.TASK
        names = [row.name for row in _referencing(bp, include_archived=False) if row.kind == users_of]
        if names:
            label = "jarayonlarda" if users_of == BlueprintKind.PROCESS else "vazifa shablonlarida"
            reasons.append(f"Quyidagi {label} ishlatilmoqda: {', '.join(names[:10])}.")
    return reasons


def archive_blueprint(*, actor: User, pk: int) -> Blueprint:
    with transaction.atomic():
        bp = _lock(pk)
        if bp.status == BlueprintStatus.ARCHIVED:
            raise _bad(MSG_ALREADY_ARCHIVED)
        reasons = archive_blockers(bp)
        if reasons:
            raise Conflict("Arxivlab bo‘lmaydi. " + " ".join(reasons))
        _status_change(bp, BlueprintStatus.ARCHIVED, actor, pending=None, pending_by=None, pending_at=None)
    return bp


def restore_blueprint(*, actor: User, pk: int) -> Blueprint:
    with transaction.atomic():
        bp = _lock(pk)
        if bp.status != BlueprintStatus.ARCHIVED:
            raise _bad(MSG_RESTORE_ARCHIVED_ONLY)
        _status_change(bp, BlueprintStatus.DRAFT, actor)
    return bp


def revert_blueprint(*, actor: User, pk: int, version: int) -> Blueprint:
    with transaction.atomic():
        bp = _lock(pk)
        row = ConfigVersion.objects.filter(object_type=BLUEPRINT_OBJECT, object_id=bp.pk, version=version).first()
        if row is None:
            raise _bad(MSG_VERSION_NOT_FOUND, "version")
        snapshot = row.data or {}
        changes = {
            "name": snapshot.get("name", bp.name),
            "description": snapshot.get("description", bp.description),
            "data": snapshot.get("data", bp.data),
            "comment": f"{version}-versiyaga qaytarildi",
        }
        return _change(bp, changes, actor)


def duplicate_blueprint(*, actor: User, pk: int) -> Blueprint:
    source = Blueprint.objects.filter(pk=pk).first()
    if source is None:
        raise NotFound("Topilmadi.")
    name = f"{source.name[: 200 - len(' (nusxa)')]} (nusxa)"
    return create_blueprint(actor=actor, kind=source.kind, name=name, description=source.description,
                            data=source.data)


def delete_blueprint(*, actor: User, pk: int) -> None:
    with transaction.atomic():
        bp = _lock(pk)
        if bp.status != BlueprintStatus.DRAFT:
            raise Conflict(MSG_DELETE_DRAFT_ONLY)
        used = (
            UserAccess.objects.filter(profile=bp).exists()
            or TaskInstance.objects.filter(template=bp).exists()
            or FormSubmission.objects.filter(form=bp).exists()
            or ProcessRun.objects.filter(process=bp).exists()
            or _referencing(bp, include_archived=True)
        )
        if used:
            raise Conflict(MSG_DELETE_REFERENCED)
        audit.record(module=AuditModule.BUILDER, action=AuditAction.DELETE, obj=bp, actor=actor,
                     before=audit.snapshot(bp))
        ConfigVersion.objects.filter(object_type=BLUEPRINT_OBJECT, object_id=bp.pk).delete()
        bp.delete()


def usage_map(blueprints: list[Blueprint]) -> dict[int, dict[str, Any]]:
    """``usage`` of each blueprint (§1.1): runtime counts + ``referenced_by`` (a few queries in total)."""
    ids = [bp.pk for bp in blueprints]
    if not ids:
        return {}

    def counts(qs: QuerySet, field: str) -> dict[int, int]:
        rows = qs.filter(**{f"{field}__in": ids}).values(field).annotate(n=Count("id"))
        return {row[field]: row["n"] for row in rows}

    users = counts(UserAccess.objects.all(), "profile_id")
    tasks = counts(TaskInstance.objects.all(), "template_id")
    submissions = counts(FormSubmission.objects.all(), "form_id")
    runs = counts(ProcessRun.objects.all(), "process_id")
    reverse: dict[int, list[dict[str, Any]]] = defaultdict(list)
    wanted = set(ids)
    for row in Blueprint.objects.filter(
        kind__in=[BlueprintKind.TASK, BlueprintKind.PROCESS, BlueprintKind.FORM]
    ).only("id", "kind", "name", "data").order_by("id"):
        for target_ids in schemas.refs_in(row.data, row.kind).values():
            for target in target_ids & wanted:
                if target != row.pk:
                    reverse[target].append({"id": row.pk, "kind": row.kind, "name": row.name})
    return {
        pk: {
            "users": users.get(pk, 0),
            "tasks": tasks.get(pk, 0),
            "submissions": submissions.get(pk, 0),
            "runs": runs.get(pk, 0),
            "referenced_by": reverse.get(pk, []),
        }
        for pk in ids
    }


# --- Role references -----------------------------------------------------------------------------


def ref_display(ref: str, profiles: dict[int, str] | None = None) -> str:
    if not ref:
        return ""
    if ref in ALL_ROLES:
        return Role(ref).label
    pid = profile_id_of(ref)
    if pid is None:
        return ref
    if profiles is not None and pid in profiles:
        return profiles[pid]
    name = Blueprint.objects.filter(pk=pid).values_list("name", flat=True).first()
    return name or ref


def user_refs(user: User) -> list[str]:
    """Role references the user matches: the effective role plus ``profile:<id>``."""
    refs = [role_of(user)] if role_of(user) else []
    access = access_of(user)
    if access is not None and access.profile_id:
        refs.append(f"{PROFILE_PREFIX}{access.profile_id}")
    return refs


def users_for_refs(refs, household: Household | None = None) -> list[User]:
    """Active users matching any of ``refs`` (``OWNER`` = only the owner of ``household``)."""
    from .access import users_for_ref_q

    condition = Q(pk__in=[])
    for ref in refs or ():
        if not ref:
            continue
        if ref == Role.OWNER:
            if household is not None:
                condition |= Q(pk=household.owner_id, is_active=True)
            continue
        condition |= users_for_ref_q(ref)
    return list(User.objects.filter(condition).order_by("id"))


# --- User access (§2) ----------------------------------------------------------------------------


def set_user_access(
    *, actor: User, user_id: int, profile_id: int | None, districts: list[str], household_ids: list[int]
) -> UserAccess:
    with transaction.atomic():
        user = User.objects.select_for_update().filter(pk=user_id, deleted_at__isnull=True).first()
        if user is None or (user.role == Role.OWNER and not user.is_superuser):
            raise NotFound("Foydalanuvchi topilmadi.")
        profile = None
        if profile_id is not None:
            profile = Blueprint.objects.filter(
                pk=profile_id, kind=BlueprintKind.ROLE, status=BlueprintStatus.ACTIVE
            ).first()
            if profile is None:
                raise _bad(MSG_PROFILE_ACTIVE_ONLY, "profile_id")
            if user.is_superuser:
                raise _bad(MSG_PROFILE_SUPERUSER, "profile_id")
        ids = sorted(set(household_ids))
        households = list(Household.objects.filter(pk__in=ids))
        if len(households) != len(ids):
            raise _bad(MSG_HOUSEHOLD_NOT_FOUND, "household_ids")
        access, created = UserAccess.objects.select_for_update().get_or_create(user=user)
        before = None if created else audit.snapshot(access)
        old_profile = None if created else access.profile_id
        access.profile = profile
        access.districts = districts
        access.updated_by = actor
        access.save()
        access.households.set(households)
        if profile is not None and profile.pk != old_profile:
            _set_role_and_revoke(user, profile.data.get("base_role", user.role), actor)
        elif profile is None and old_profile is not None:
            _set_role_and_revoke(user, user.role, actor)  # the profile's limits are gone: new session
        if created:
            audit.record_create(AuditModule.BUILDER, access, actor)
        else:
            audit.record_update(AuditModule.BUILDER, access, actor, before)
        if hasattr(user, "_nnp_access_cache"):
            del user._nnp_access_cache
    return access


# --- Runtime helpers -----------------------------------------------------------------------------


def usable(bp: Blueprint, user: User) -> bool:
    """Active, or test for a Super Admin (§1 test mode)."""
    return bp.status == BlueprintStatus.ACTIVE or (bp.status == BlueprintStatus.TEST and is_super_admin(user))


def _runtime_blueprint(pk: Any, kind: str, user: User, field: str, message: str) -> Blueprint:
    bp = Blueprint.objects.filter(pk=pk, kind=kind).first() if pk is not None else None
    if bp is None or not usable(bp, user):
        raise _bad(message, field)
    return bp


def resolve_household(actor: User, household_id: int | None, *, required: bool = False) -> Household | None:
    """OWNER: always his own household; others: a household in their scope (§2)."""
    if role_of(actor) == Role.OWNER:
        own = Household.objects.select_related("owner").filter(owner=actor).first()
        if own is None or (household_id is not None and household_id != own.pk):
            raise _bad(MSG_HOUSEHOLD_NOT_FOUND, "household_id")
        return own
    if household_id is None:
        if required:
            raise _bad(MSG_REQUIRED, "household_id")
        return None
    household = scope_households(actor, Household.objects.select_related("owner")).filter(pk=household_id).first()
    if household is None:
        raise _bad(MSG_HOUSEHOLD_NOT_FOUND, "household_id")
    if not household.is_active:
        raise _bad(MSG_HOUSEHOLD_INACTIVE, "household_id")
    return household


def visible_flag(qs: QuerySet, user: User) -> QuerySet:
    """``is_test`` rows exist only for Super Admins (§1)."""
    return qs if is_super_admin(user) else qs.filter(is_test=False)


# --- Forms (§3.1) --------------------------------------------------------------------------------


def can_fill(user: User, form: Blueprint) -> bool:
    if is_super_admin(user):
        return True
    roles = (form.data or {}).get("roles") or []
    if not roles:
        return role_of(user) != Role.OWNER
    return matches_any(user, roles)


def fillable_forms(user: User) -> list[Blueprint]:
    statuses = [BlueprintStatus.ACTIVE] + ([BlueprintStatus.TEST] if is_super_admin(user) else [])
    qs = Blueprint.objects.filter(kind=BlueprintKind.FORM, status__in=statuses).order_by("name", "id")
    return [form for form in qs if can_fill(user, form)]


def form_snapshot(form: Blueprint) -> dict[str, Any]:
    data = form.data or {}
    return {"id": form.pk, "name": form.name, "version": form.version, "roles": data.get("roles", []),
            "fields": data.get("fields", [])}


def _create_submission(
    *,
    actor: User,
    form_id: int,
    form_version: int,
    fields: list[dict],
    values: Any,
    household: Household | None,
    task: TaskInstance | None,
    is_test: bool,
    previous: FormSubmission | None = None,
) -> FormSubmission:
    reusable = []
    if task is not None:
        reusable = list(FormSubmission.objects.filter(task=task).values_list("pk", flat=True))
    checker = schemas.FileChecker(actor, reusable)
    cleaned = schemas.validate_values(
        fields, values, actor, previous=previous.data if previous is not None else None, files=checker
    )
    submission = FormSubmission.objects.create(
        form_id=form_id,
        form_version=form_version,
        fields=fields,
        data=cleaned,
        household=household,
        task=task,
        is_test=is_test,
        submitted_by=actor,
    )
    if checker.new_ids:
        files.bind_files(
            checker.new_ids,
            actor=actor,
            purpose=AttachmentPurpose.FORM_FILE,
            obj=submission,
            household=household,
            object_type=schemas.SUBMISSION_OBJECT_TYPE,
        )
    audit.record_create(AuditModule.BUILDER, submission, actor)
    return submission


def submit_form(*, actor: User, form_id: int, household_id: int | None, data: Any) -> FormSubmission:
    form = Blueprint.objects.filter(pk=form_id, kind=BlueprintKind.FORM).first()
    if form is None or not usable(form, actor):
        raise NotFound(MSG_FORM_NOT_FOUND)
    if not can_fill(actor, form):
        raise PermissionDenied(MSG_FORM_NOT_ALLOWED)
    household = resolve_household(actor, household_id)
    with transaction.atomic():
        return _create_submission(
            actor=actor,
            form_id=form.pk,
            form_version=form.version,
            fields=list((form.data or {}).get("fields", [])),
            values=data,
            household=household,
            task=None,
            is_test=form.status == BlueprintStatus.TEST,
        )


def submissions_visible(user: User) -> QuerySet[FormSubmission]:
    qs = FormSubmission.objects.select_related("form", "household", "household__owner", "submitted_by")
    qs = visible_flag(qs, user)
    role = role_of(user)
    if role == Role.OWNER:
        qs = qs.filter(Q(household__owner=user) | Q(submitted_by=user))
    elif role != Role.ADMIN:
        qs = qs.filter(Q(submitted_by=user) | Q(task__in=tasks_visible(user)))
    return scope_households(user, qs, "household")


def can_view_submission_file(user: User, attachment) -> bool:
    """``core.files`` visibility rule for ``form_submission`` attachments."""
    return submissions_visible(user).filter(pk=attachment.object_id).exists()


# --- Tasks (§3.2) --------------------------------------------------------------------------------


def tasks_visible(user: User) -> QuerySet[TaskInstance]:
    qs = TaskInstance.objects.select_related(
        "template", "household", "household__owner", "assigned_to", "process_run", "submitted_by", "decided_by"
    )
    qs = visible_flag(qs, user)
    role = role_of(user)
    if role == Role.OWNER:
        qs = qs.filter(household__owner=user)
    elif role != Role.ADMIN:
        refs = user_refs(user)
        qs = qs.filter(
            Q(assigned_to=user)
            | Q(assigned_to__isnull=True, executor__in=refs)
            | Q(approver__in=refs)
            | Q(submitted_by=user)
        )
    return scope_households(user, qs, "household")


def mine_q(user: User) -> Q:
    return Q(assigned_to=user) | Q(assigned_to__isnull=True, executor__in=user_refs(user))


def approve_q(user: User) -> Q:
    if is_super_admin(user):
        return Q(status=TaskStatus.SUBMITTED) & ~Q(approver="")
    return Q(status=TaskStatus.SUBMITTED, approver__in=user_refs(user))


def task_counts(user: User) -> dict[str, int]:
    qs = tasks_visible(user)
    return {
        "mine_open": qs.filter(mine_q(user), status__in=[TaskStatus.OPEN, TaskStatus.RETURNED]).count(),
        "to_approve": qs.filter(approve_q(user)).count(),
    }


def is_executor(user: User, task: TaskInstance) -> bool:
    if is_super_admin(user) and task.is_test:
        return True
    if task.assigned_to_id:
        return task.assigned_to_id == user.pk
    if task.executor == Role.OWNER:
        return task.household is not None and task.household.owner_id == user.pk
    return matches_ref(user, task.executor)


def is_approver(user: User, task: TaskInstance) -> bool:
    if not task.approver:
        return False
    if is_super_admin(user):
        return True
    if task.approver == Role.OWNER:
        return task.household is not None and task.household.owner_id == user.pk
    return matches_ref(user, task.approver)


def can_submit(user: User, task: TaskInstance) -> bool:
    return task.status in (TaskStatus.OPEN, TaskStatus.RETURNED) and is_executor(user, task)


def can_approve(user: User, task: TaskInstance) -> bool:
    return task.status == TaskStatus.SUBMITTED and is_approver(user, task)


def can_cancel(user: User, task: TaskInstance) -> bool:
    return role_of(user) == Role.ADMIN and task.status not in (TaskStatus.APPROVED, TaskStatus.CANCELLED)


def is_overdue(task: TaskInstance, today: date | None = None) -> bool:
    today = today or timezone.localdate()
    return bool(task.due_on and task.due_on < today and task.status in (TaskStatus.OPEN, TaskStatus.RETURNED))


def _new_task(
    *,
    template: Blueprint,
    actor: User,
    household: Household | None,
    assigned_to: User | None = None,
    starts_on: date | None = None,
    due_on: date | None = None,
    note: str = "",
    run: ProcessRun | None = None,
    stage_index: int | None = None,
    is_test: bool = False,
) -> TaskInstance:
    data = template.data or {}
    definition = {**data, "template_name": template.name, "note": note, "form": None}
    if data.get("form_id"):
        form = Blueprint.objects.filter(pk=data["form_id"], kind=BlueprintKind.FORM).first()
        if form is not None:
            definition["form"] = form_snapshot(form)
    starts_on = starts_on or (date.fromisoformat(data["start_date"]) if data.get("start_date") else None)
    starts_on = starts_on or timezone.localdate()
    due_on = due_on or schemas.due_date(starts_on, data)
    if due_on is not None and due_on < starts_on:
        raise _bad(MSG_DUE_BEFORE_START, "due_on")
    task = TaskInstance.objects.create(
        template=template,
        template_version=template.version,
        title=template.name[:200],
        executor=data.get("executor", ""),
        approver=data.get("approver", "") or "",
        assigned_to=assigned_to,
        household=household,
        process_run=run,
        stage_index=stage_index,
        definition=definition,
        starts_on=starts_on,
        due_on=due_on,
        is_test=is_test or template.status == BlueprintStatus.TEST,
        created_by=actor,
    )
    audit.record_create(AuditModule.BUILDER, task, actor)
    events.task_assigned(task)
    return task


def create_task(
    *,
    actor: User,
    template_id: int,
    household_id: int | None = None,
    assigned_to_id: int | None = None,
    starts_on: date | None = None,
    due_on: date | None = None,
    note: str = "",
) -> TaskInstance:
    template = _runtime_blueprint(template_id, BlueprintKind.TASK, actor, "template_id", MSG_TEMPLATE_NOT_FOUND)
    data = template.data or {}
    household = resolve_household(actor, household_id, required=bool(data.get("requires_household")))
    assigned_to = None
    if assigned_to_id is not None:
        assigned_to = User.objects.filter(pk=assigned_to_id, is_active=True, deleted_at__isnull=True).first()
        if assigned_to is None or (
            data.get("executor") == Role.OWNER
            and (household is None or household.owner_id != assigned_to.pk)
        ) or (data.get("executor") != Role.OWNER and not matches_ref(assigned_to, data.get("executor", ""))):
            raise _bad(MSG_ASSIGNEE, "assigned_to_id")
    if starts_on and due_on and due_on < starts_on:
        raise _bad(MSG_DUE_BEFORE_START, "due_on")
    with transaction.atomic():
        return _new_task(template=template, actor=actor, household=household, assigned_to=assigned_to,
                         starts_on=starts_on, due_on=due_on, note=note)


def latest_submission(task: TaskInstance) -> FormSubmission | None:
    return FormSubmission.objects.filter(task=task).order_by("-id").first()


def _lock_task(pk: int, user: User) -> tuple[TaskInstance, ProcessRun | None]:
    """Lock order: run → task. 404 when the user cannot see the task."""
    row = tasks_visible(user).filter(pk=pk).values("pk", "process_run_id").first()
    if row is None:
        raise NotFound("Vazifa topilmadi.")
    run = None
    if row["process_run_id"]:
        run = ProcessRun.objects.select_for_update().get(pk=row["process_run_id"])
    # of=("self",): household is nullable (LEFT JOIN) — PostgreSQL refuses FOR UPDATE on the nullable side.
    task = TaskInstance.objects.select_for_update(of=("self",)).select_related("household", "household__owner").get(pk=pk)
    return task, run


def _task_conflict(task: TaskInstance) -> Conflict:
    return Conflict(f"Vazifa holati: {task.get_status_display()}. Amalni bajarib bo‘lmaydi.")


def submit_task(*, actor: User, pk: int, data: Any) -> TaskInstance:
    with transaction.atomic():
        task, run = _lock_task(pk, actor)
        if task.status not in (TaskStatus.OPEN, TaskStatus.RETURNED):
            raise _task_conflict(task)
        if not is_executor(actor, task):
            raise PermissionDenied(MSG_NOT_EXECUTOR)
        form = (task.definition or {}).get("form")
        submission = None
        if form:
            submission = _create_submission(
                actor=actor,
                form_id=form["id"],
                form_version=form.get("version", 1),
                fields=list(form.get("fields", [])),
                values=data,
                household=task.household,
                task=task,
                is_test=task.is_test,
                previous=latest_submission(task),
            )
        before = audit.snapshot(task)
        now = timezone.now()
        task.submitted_by, task.submitted_at = actor, now
        if task.approver:
            task.status = TaskStatus.SUBMITTED
        else:
            task.status = TaskStatus.APPROVED
            task.decided_by, task.decided_at = actor, now
        task.save()
        audit.record(module=AuditModule.BUILDER, action=AuditAction.STATUS_CHANGE, obj=task, actor=actor,
                     before={"status": before["status"]},
                     after={"status": task.status, "submission_id": submission.pk if submission else None})
        if task.status == TaskStatus.SUBMITTED:
            events.task_submitted(task)
        _advance(run, task, actor)
    return task


def approve_task(*, actor: User, pk: int, note: str = "") -> TaskInstance:
    with transaction.atomic():
        task, run = _lock_task(pk, actor)
        if task.status != TaskStatus.SUBMITTED:
            raise _task_conflict(task)
        if not is_approver(actor, task):
            raise PermissionDenied(MSG_NOT_APPROVER)
        task.status = TaskStatus.APPROVED
        task.decided_by, task.decided_at, task.decision_note = actor, timezone.now(), note
        task.save()
        audit.record(module=AuditModule.BUILDER, action=AuditAction.APPROVE, obj=task, actor=actor,
                     before={"status": TaskStatus.SUBMITTED}, after={"status": task.status, "note": note})
        events.task_approved(task)
        _advance(run, task, actor)
    return task


def return_task(*, actor: User, pk: int, note: str) -> TaskInstance:
    note = (note or "").strip()
    if not note:
        raise _bad(MSG_NOTE_REQUIRED, "note")
    with transaction.atomic():
        task, _run = _lock_task(pk, actor)
        if task.status != TaskStatus.SUBMITTED:
            raise _task_conflict(task)
        if not is_approver(actor, task):
            raise PermissionDenied(MSG_NOT_APPROVER)
        task.status = TaskStatus.RETURNED
        task.decided_by, task.decided_at, task.decision_note = actor, timezone.now(), note
        task.save()
        audit.record(module=AuditModule.BUILDER, action=AuditAction.STATUS_CHANGE, obj=task, actor=actor,
                     before={"status": TaskStatus.SUBMITTED}, after={"status": task.status, "note": note})
        events.task_returned(task)
    return task


def cancel_task(*, actor: User, pk: int, note: str = "") -> TaskInstance:
    if role_of(actor) != Role.ADMIN:
        raise PermissionDenied(MSG_NO_PERMISSION)
    with transaction.atomic():
        task, _run = _lock_task(pk, actor)
        if task.status in (TaskStatus.APPROVED, TaskStatus.CANCELLED):
            raise _task_conflict(task)
        _cancel_task_row(task, actor, note)
    return task


def _cancel_task_row(task: TaskInstance, actor: User, note: str) -> None:
    old = task.status
    task.status = TaskStatus.CANCELLED
    task.decided_by, task.decided_at, task.decision_note = actor, timezone.now(), (note or "")[:1000]
    task.save()
    audit.record(module=AuditModule.BUILDER, action=AuditAction.STATUS_CHANGE, obj=task, actor=actor,
                 before={"status": old}, after={"status": task.status, "note": note})


# --- Processes (§3.3) ----------------------------------------------------------------------------


def runs_visible(user: User) -> QuerySet[ProcessRun]:
    qs = ProcessRun.objects.select_related("process", "household", "household__owner", "started_by")
    qs = visible_flag(qs, user)
    role = role_of(user)
    if role == Role.OWNER:
        qs = qs.filter(household__owner=user)
    elif role != Role.ADMIN:
        qs = qs.filter(pk__in=tasks_visible(user).filter(process_run__isnull=False).values("process_run_id"))
    return scope_households(user, qs, "household")


def _stage_template(run: ProcessRun, index: int) -> Blueprint | None:
    """The stage's task template when it can still be used, else ``None`` (blocked stage)."""
    stage = run.stages[index] if 0 <= index < len(run.stages) else None
    template = Blueprint.objects.filter(pk=(stage or {}).get("task_id"), kind=BlueprintKind.TASK).first()
    if template is None:
        return None
    allowed = {BlueprintStatus.ACTIVE} | ({BlueprintStatus.TEST} if run.is_test else set())
    return template if template.status in allowed else None


def start_process(*, actor: User, process_id: int, household_id: int | None = None) -> ProcessRun:
    process = _runtime_blueprint(process_id, BlueprintKind.PROCESS, actor, "process_id", MSG_PROCESS_NOT_FOUND)
    stages = list((process.data or {}).get("stages", []))
    templates = {
        row.pk: row
        for row in Blueprint.objects.filter(pk__in=[s.get("task_id") for s in stages], kind=BlueprintKind.TASK)
    }
    needs_household = any(
        (templates[stage["task_id"]].data or {}).get("requires_household")
        for stage in stages
        if stage.get("task_id") in templates
    )
    household = resolve_household(actor, household_id, required=needs_household)
    with transaction.atomic():
        run = ProcessRun.objects.create(
            process=process,
            process_version=process.version,
            stages=stages,
            household=household,
            is_test=process.status == BlueprintStatus.TEST,
            started_by=actor,
        )
        template = _stage_template(run, 0)
        if template is None:
            raise _bad(MSG_STAGE_UNAVAILABLE.format(index=1), "process_id")
        audit.record_create(AuditModule.BUILDER, run, actor)
        _new_task(template=template, actor=actor, household=household, run=run, stage_index=0, is_test=run.is_test)
        events.stage_started(run, 0)
    return run


def _condition_reached(run: ProcessRun, task: TaskInstance) -> bool:
    stage = run.stages[task.stage_index] if task.stage_index is not None and task.stage_index < len(run.stages) else {}
    condition = stage.get("condition") or (task.definition or {}).get("next_condition") or "approved"
    if condition == "submitted":
        return task.status in (TaskStatus.SUBMITTED, TaskStatus.APPROVED)
    return task.status == TaskStatus.APPROVED


def _advance(run: ProcessRun | None, task: TaskInstance, actor: User) -> None:
    """Move ``run`` to the next stage when ``task`` (the current stage task) reached its condition."""
    if run is None or run.status != ProcessRunStatus.RUNNING or task.stage_index != run.current_index:
        return
    if not _condition_reached(run, task):
        return
    next_index = run.current_index + 1
    if next_index >= len(run.stages):
        run.status = ProcessRunStatus.DONE
        run.finished_at = timezone.now()
        run.save(update_fields=["status", "finished_at"])
        audit.record(module=AuditModule.BUILDER, action=AuditAction.STATUS_CHANGE, obj=run, actor=actor,
                     before={"status": ProcessRunStatus.RUNNING}, after={"status": run.status})
        events.process_finished(run)
        return
    run.current_index = next_index
    run.save(update_fields=["current_index"])
    template = _stage_template(run, next_index)
    if template is None:
        audit.record(module=AuditModule.BUILDER, action=AuditAction.UPDATE, obj=run, actor=actor,
                     after={"current_index": next_index, "blocked": True,
                            "reason": MSG_STAGE_UNAVAILABLE.format(index=next_index + 1)})
        events.process_blocked(run, next_index)
        return
    audit.record(module=AuditModule.BUILDER, action=AuditAction.UPDATE, obj=run, actor=actor,
                 after={"current_index": next_index})
    _new_task(template=template, actor=actor, household=run.household, run=run, stage_index=next_index,
              is_test=run.is_test)
    events.stage_started(run, next_index)


def cancel_process(*, actor: User, pk: int, reason: str = "") -> ProcessRun:
    with transaction.atomic():
        run = runs_visible(actor).select_for_update(of=("self",)).filter(pk=pk).first()
        if run is None:
            raise NotFound("Jarayon topilmadi.")
        if run.status != ProcessRunStatus.RUNNING:
            raise Conflict(f"Jarayon holati: {run.get_status_display()}. Amalni bajarib bo‘lmaydi.")
        for task in TaskInstance.objects.select_for_update().filter(
            process_run=run, status__in=[TaskStatus.OPEN, TaskStatus.RETURNED, TaskStatus.SUBMITTED]
        ).order_by("id"):
            _cancel_task_row(task, actor, reason or "Jarayon bekor qilindi")
        run.status = ProcessRunStatus.CANCELLED
        run.cancel_reason = (reason or "")[:500]
        run.finished_at = timezone.now()
        run.save(update_fields=["status", "cancel_reason", "finished_at"])
        audit.record(module=AuditModule.BUILDER, action=AuditAction.STATUS_CHANGE, obj=run, actor=actor,
                     before={"status": ProcessRunStatus.RUNNING}, after={"status": run.status, "reason": reason})
    return run

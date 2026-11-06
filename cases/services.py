"""The chain logic: who may do what on which stage, and what each action changes.

Every action runs in one transaction with the case row locked, writes a StageEvent and an audit row,
and queues the Telegram notifications (on commit).
"""

from datetime import timedelta
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from accounts.models import Role, User, make_temp_password
from audit.models import log
from notify import services as notify
from stock import services as stock

from . import stages as chain
from .models import Case, CaseStage, CaseStatus, Document, Photo, StageEvent, StageStatus

OPEN = (StageStatus.ACTIVE, StageStatus.RETURNED)
HOUSEHOLD_FIELDS = ("name", "owner_full_name", "owner_phone", "region", "district", "mfy", "address",
                    "latitude", "longitude")


class ChainError(Exception):
    """Refused action. `status` 400/403/404/409; `extra` is merged into the JSON body."""

    def __init__(self, detail: str, status: int = 409, **extra):
        super().__init__(detail)
        self.detail, self.status, self.extra = detail, status, extra


MSG_FORBIDDEN = "Sizda bu amal uchun ruxsat yo‘q."
MSG_NOT_CURRENT = "Bu bosqich hozir faol emas. Bosqichlarni o‘tkazib yuborish mumkin emas."
MSG_CLOSED = "Xonadon jarayoni yakunlangan yoki rad etilgan."


# --- Permissions --------------------------------------------------------------------------------

def visible_cases(user: User):
    qs = Case.objects.all()
    if user.role == Role.OWNER:
        qs = qs.filter(owner_user=user)
    return qs


def can_execute(user: User, case: Case, number: int) -> bool:
    """Only the stage's own role does the work ("Bajarildi" / "Tasdiqlash").

    Admin / Super Admin never act on someone else's behalf: they approve, return or reject.
    """
    if number == chain.OWNER_STAGE:
        return user.role == Role.OWNER and case.owner_user_id == user.pk
    return user.role == chain.get(number).executor


def can_approve(user: User, number: int) -> bool:
    approver = chain.get(number).approver
    return approver is not None and user.role in (approver, Role.SUPERADMIN)


def can_reject(user: User, case: Case, stage: CaseStage) -> bool:
    if case.status != CaseStatus.IN_PROGRESS:
        return False
    if user.is_admin:
        return True
    return user.role == Role.DIRECTOR and stage.number == 3 and stage.status == StageStatus.ACTIVE


def can_flags(user: User, case: Case, stage: CaseStage) -> dict:
    """What the caller may do now on the case's current stage."""
    flags = {"submit": False, "approve": False, "return": False, "reject": False}
    if case.status == CaseStatus.IN_PROGRESS:
        n, has_approver = stage.number, chain.get(stage.number).approver is not None
        executor, approver = can_execute(user, case, n), can_approve(user, n)
        flags["submit"] = stage.status in OPEN and executor
        flags["approve"] = stage.status == StageStatus.SUBMITTED and approver
        flags["return"] = (stage.status == StageStatus.SUBMITTED and approver) or (
            stage.status == StageStatus.ACTIVE and _can_send_back(n) and executor)
        flags["reject"] = can_reject(user, case, stage)
    flags["owner_account"] = user.is_admin
    flags["assign_vet"] = user.is_admin
    flags["create_request"] = flags["request"] = (
        user.role == Role.OWNER and case.owner_user_id == user.pk and in_growing_period(case, stage))
    return flags


def _can_send_back(number: int) -> bool:
    """An ACTIVE stage without an approver may send the previous stage back (HR, Director); not growing."""
    return chain.get(number).approver is None and 1 < number and number != chain.GROWING_STAGE


def in_growing_period(case: Case, stage: CaseStage | None = None) -> bool:
    """The case is in the growing period: stage 10 is ACTIVE (requests and weekly visits happen then)."""
    if case.status != CaseStatus.IN_PROGRESS or case.current_stage != chain.GROWING_STAGE:
        return False
    if stage is None or stage.number != chain.GROWING_STAGE:
        stage = case.stages.get(number=chain.GROWING_STAGE)
    return stage.status == StageStatus.ACTIVE


def my_stages_q(user: User) -> Q:
    """CaseStage filter: stages where `user` must act now (the `mine=1` list)."""
    role = user.role
    q = Q(pk__in=[])
    if role == Role.OWNER:
        q = Q(status__in=OPEN, number=chain.OWNER_STAGE, case__owner_user=user)
    else:
        numbers = [s.number for s in chain.STAGES if s.executor == role and s.number != chain.OWNER_STAGE]
        if numbers:
            q |= Q(status__in=OPEN, number__in=numbers)
    if role == Role.ADMIN:
        q |= Q(status__in=OPEN, number=chain.OWNER_STAGE, case__owner_user__isnull=True)
        q |= Q(status__in=OPEN, number=1, case__created_by=user)
    approvals = [s.number for s in chain.STAGES if s.approver == role]
    if approvals:
        q |= Q(status=StageStatus.SUBMITTED, number__in=approvals)
    return Q(case__status=CaseStatus.IN_PROGRESS, case__deleted_at__isnull=True) & q


def my_cases(user: User):
    return visible_cases(user).filter(id__in=CaseStage.objects.filter(my_stages_q(user)).values("case_id"))


# --- Recipients ---------------------------------------------------------------------------------

def _role_users(*roles):
    return list(User.objects.active_with_role(*roles))


def executor_users(case: Case, number: int) -> list[User]:
    if number == chain.OWNER_STAGE:
        owner = case.owner_user
        if owner is not None and owner.is_active and owner.deleted_at is None:
            return [owner]
        return _role_users(Role.ADMIN)
    return _role_users(chain.get(number).executor)


def _notify(kind: str, users, case: Case, stage: CaseStage, event_key: str, note: str = "") -> None:
    notify.queue(
        users,
        text=notify.compose(kind, case, stage.number, note=note,
                            due_at=stage.due_at if kind in ("active", "returned", "overdue") else None),
        button_path=f"/cases/{case.pk}",
        event_key=event_key,
    )


# --- Helpers ------------------------------------------------------------------------------------

def _lock(case_id: int) -> Case:
    try:
        return Case.objects.select_for_update().get(pk=case_id)  # no select_related: plain row lock
    except Case.DoesNotExist:
        raise ChainError("Xonadon topilmadi.", 404) from None


def _event(case: Case, number: int, action: str, user: User, note: str = "", **audit_after) -> StageEvent:
    event = StageEvent.objects.create(case=case, stage_number=number, action=action, actor=user, note=note)
    log(user, f"case.{action}", case, after={"stage": number, "note": note, **audit_after})
    return event


def _open(stage: CaseStage, status: str, now) -> None:
    stage.status = status
    stage.activated_at = now
    stage.due_at = now + timedelta(days=chain.days_of(stage.number))
    stage.overdue_notified_on = None


def _activate_next(case: Case, number: int, user: User, now, event: StageEvent) -> None:
    """Stage `number` is DONE: open the next stage, or finish the case after the last one."""
    if number == chain.LAST:
        case.status = CaseStatus.DONE
        case.done_at = now
        case.save(update_fields=["status", "done_at"])
        users = _role_users(Role.SUPERADMIN, Role.ADMIN) + ([case.owner_user] if case.owner_user else [])
        _notify("done", users, case, case.stages.get(number=number), f"ev{event.pk}:done")
        return
    if number == chain.GROWING_STAGE:  # sent to the slaughterhouse: routine visits are no longer needed
        from .growing import cancel_routine_visits

        cancel_routine_visits(case, user, now)
    nxt = case.stages.get(number=number + 1)
    _open(nxt, StageStatus.ACTIVE, now)
    nxt.save()
    case.current_stage = nxt.number
    case.save(update_fields=["current_stage"])
    _notify("active", executor_users(case, nxt.number), case, nxt, f"ev{event.pk}:active")


def _attach(model, ids, user: User, case: Case, number: int) -> None:
    ids = list(dict.fromkeys(int(i) for i in ids or []))
    if not ids:
        return
    rows = list(model.objects.select_for_update(of=("self",)).filter(pk__in=ids))
    label = "Rasm" if model is Photo else "Hujjat"
    found = {row.pk for row in rows}
    for row in rows:
        own_new = row.case_id is None and row.uploaded_by_id == user.pk
        same = row.case_id == case.pk and row.stage == number
        if not (own_new or same):
            found.discard(row.pk)
    missing = [i for i in ids if i not in found]
    if missing:
        raise ChainError(f"{label} topilmadi yoki boshqa xonadonga biriktirilgan: {missing}", 400)
    model.objects.filter(pk__in=ids, case__isnull=True).update(case=case, stage=number)


def _clean_data(stage_def: chain.Stage, data: dict) -> dict:
    data = data if isinstance(data, dict) else {}
    clean, errors = {}, {}
    for f in stage_def.fields:
        value = data.get(f.name)
        if value in (None, ""):
            if f.required:
                errors[f.name] = ["Bu maydon to‘ldirilishi shart."]
            continue
        if f.kind == "int":
            try:
                value = int(value)
            except (TypeError, ValueError):
                errors[f.name] = ["Butun son kiriting."]
                continue
            if f.min_value is not None and value < f.min_value:
                errors[f.name] = [f"Qiymat {f.min_value} dan kichik bo‘lmasligi kerak."]
                continue
        elif f.kind == "decimal":
            try:
                value = Decimal(str(value).replace(",", ".")).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
            except (InvalidOperation, ValueError):
                errors[f.name] = ["Son kiriting (masalan 245.50)."]
                continue
            if not value.is_finite() or value > Decimal("1e9"):
                errors[f.name] = ["Son kiriting (masalan 245.50)."]
                continue
            if f.min_value is not None and (value <= f.min_value if f.exclusive_min else value < f.min_value):
                errors[f.name] = [f"Qiymat {f.min_value} dan katta bo‘lishi kerak." if f.exclusive_min
                                  else f"Qiymat {f.min_value} dan kichik bo‘lmasligi kerak."]
                continue
            value = float(value)  # JSON number, 2 dp
        elif f.kind == "choice":
            if value not in {c[0] for c in f.choices}:
                errors[f.name] = [f"“{value}” ruxsat etilgan qiymat emas."]
                continue
        else:
            value = str(value).strip()[:2000]
        clean[f.name] = value
    if errors:
        raise ChainError("Ma’lumotlar noto‘g‘ri.", 400, errors=errors)
    return clean


def _stage_data(case: Case, number: int) -> dict:
    stage = case.stages.filter(number=number).first()
    return (stage.data or {}) if stage else {}


def _cross_check(case: Case, number: int, clean: dict) -> None:
    """Rules between stages (v2.1). Older cases without the source field are not checked."""
    if number == chain.DELIVERY_STAGE:
        given = _stage_data(case, chain.INCUBATION_STAGE).get("chicks_count")
        if isinstance(given, int) and clean["delivered_healthy"] + clean["died_in_transit"] > given:
            msg = f"Sog‘lom va nobud bo‘lganlar jami berilgan jo‘jalar sonidan ({given}) oshmasligi kerak."
            raise ChainError("Ma’lumotlar noto‘g‘ri.", 400,
                             errors={"delivered_healthy": [msg], "died_in_transit": [msg]})
    elif number == chain.OWNER_STAGE:
        delivered = _stage_data(case, chain.DELIVERY_STAGE).get("delivered_healthy")
        if isinstance(delivered, int) and clean["received_count"] > delivered:
            raise ChainError("Ma’lumotlar noto‘g‘ri.", 400, errors={"received_count": [
                f"Qabul qilinganlar yetkazilgan sog‘lom jo‘jalar sonidan ({delivered}) oshmasligi kerak."]})
    elif number == chain.ACCOUNTING_STAGE:
        if clean.get("revenue") is None:
            if clean.get("price_per_kg") is None:
                raise ChainError("Ma’lumotlar noto‘g‘ri.", 400, errors={"revenue": [
                    "Sotuv summasini yoki 1 kg narxini kiriting."]})
            kg = _stage_data(case, chain.SLAUGHTER_STAGE).get("total_kg")
            if not kg:
                raise ChainError("Ma’lumotlar noto‘g‘ri.", 400, errors={"revenue": [
                    "12-bosqichda vazn yo‘q: sotuv summasini kiriting."]})
            clean["revenue"] = int((Decimal(clean["price_per_kg"]) * Decimal(str(kg))).quantize(
                Decimal(1), rounding=ROUND_HALF_UP))


def _check_photos(case: Case, stage_def: chain.Stage) -> None:
    if stage_def.photos and Photo.objects.filter(case=case, stage=stage_def.number).count() < stage_def.photos:
        raise ChainError("Kamida bitta jonli rasm kerak.", 400, errors={"photo_ids": ["Kamida bitta rasm yuklang."]})


def _round6(value):
    return None if value is None else Decimal(str(round(float(value), 6)))


# --- Actions ------------------------------------------------------------------------------------

@transaction.atomic
def create_case(user: User, fields: dict, *, package, conclusion: str, photo_ids, document_ids) -> Case:
    if not can_execute(user, Case(), 1):
        raise ChainError(MSG_FORBIDDEN, 403)
    if not photo_ids:
        raise ChainError("Kamida bitta jonli rasm kerak.", 400, errors={"photo_ids": ["Kamida bitta rasm yuklang."]})
    now = timezone.now()
    values = {key: fields.get(key, "") for key in HOUSEHOLD_FIELDS}
    values["latitude"], values["longitude"] = _round6(fields.get("latitude")), _round6(fields.get("longitude"))
    case = Case.objects.create(**values, package=package, created_by=user)
    case.code = f"XN-{case.pk:04d}"
    case.save(update_fields=["code"])
    stages = [CaseStage(case=case, number=s.number, key=s.key) for s in chain.STAGES]
    CaseStage.objects.bulk_create(stages)
    _attach(Photo, photo_ids, user, case, 1)
    _attach(Document, document_ids, user, case, 1)

    first = case.stages.get(number=1)
    first.status = StageStatus.DONE
    first.data = {"conclusion": conclusion, "package_id": package.pk}
    first.note = conclusion
    first.activated_at = first.submitted_at = now
    first.submitted_by = user
    first.save()
    _event(case, 1, StageEvent.CREATE, user)
    event = _event(case, 1, StageEvent.SUBMIT, user, conclusion)
    _activate_next(case, 1, user, now, event)
    return case


@transaction.atomic
def submit(user: User, case_id: int, number: int, *, data: dict, note: str = "", photo_ids=(), document_ids=(),
           household: dict | None = None, package=None) -> Case:
    case = _lock(case_id)
    stage_def = _stage_def(number)
    if not can_execute(user, case, number):
        raise ChainError(MSG_FORBIDDEN, 403)
    stage = _current(case, number, OPEN)
    clean = _clean_data(stage_def, data)
    _cross_check(case, number, clean)
    _attach(Photo, photo_ids, user, case, number)
    _attach(Document, document_ids, user, case, number)
    _check_photos(case, stage_def)

    if number == 1:  # the inspector may correct the household data when resubmitting
        for key, value in (household or {}).items():
            setattr(case, key, _round6(value) if key in ("latitude", "longitude") else value)
        if package is not None:
            case.package = package
        case.save()
        clean["package_id"] = case.package_id
    if number == chain.WAREHOUSE_STAGE:
        try:
            stock.issue_package(case, user=user)
        except stock.StockShortage as exc:
            raise ChainError("Omborda yetarli mahsulot yo‘q.", 409, missing=exc.missing) from None

    if number == chain.ACCOUNTING_STAGE:  # final finance snapshot (v2.1 §6)
        from .finance import finance

        clean["finance"] = finance(case, revenue=clean["revenue"], price_per_kg=clean.get("price_per_kg"),
                                   final=True)

    now = timezone.now()
    stage.data, stage.note = clean, note or ""
    stage.submitted_by, stage.submitted_at = user, now
    stage.return_reason = ""
    event = _event(case, number, StageEvent.SUBMIT, user, note, data=clean)
    if stage_def.approver is None:
        stage.status = StageStatus.DONE
        stage.save()
        _activate_next(case, number, user, now, event)
    else:
        stage.status = StageStatus.SUBMITTED
        stage.save()
        _notify("submitted", _role_users(stage_def.approver), case, stage, f"ev{event.pk}:submitted", note)
    return case


@transaction.atomic
def approve(user: User, case_id: int, number: int, *, note: str = "") -> Case:
    case = _lock(case_id)
    _stage_def(number)
    if not can_approve(user, number):
        raise ChainError(MSG_FORBIDDEN, 403)
    stage = _current(case, number, (StageStatus.SUBMITTED,))
    now = timezone.now()
    stage.status = StageStatus.DONE
    stage.decided_by, stage.decided_at = user, now
    stage.save()
    event = _event(case, number, StageEvent.APPROVE, user, note)
    _activate_next(case, number, user, now, event)
    return case


@transaction.atomic
def return_stage(user: User, case_id: int, number: int, *, reason: str) -> Case:
    case = _lock(case_id)
    stage_def = _stage_def(number)
    if not reason.strip():
        raise ChainError("Sababni yozing.", 400, errors={"reason": ["Bu maydon to‘ldirilishi shart."]})
    now = timezone.now()
    stage = _current(case, number, (StageStatus.SUBMITTED, StageStatus.ACTIVE))
    if stage.status == StageStatus.SUBMITTED:
        if not can_approve(user, number):
            raise ChainError(MSG_FORBIDDEN, 403)
        if number == chain.WAREHOUSE_STAGE:
            stock.return_package(case, user=user)
        _open(stage, StageStatus.RETURNED, now)
        stage.return_reason, stage.decided_by, stage.decided_at = reason, user, now
        stage.save()
        target = stage
    else:  # ACTIVE stage without an approver sends the previous stage back (HR -> inspection)
        if not can_execute(user, case, number):
            raise ChainError(MSG_FORBIDDEN, 403)
        if not _can_send_back(number):
            raise ChainError("Bu bosqichdan ortga qaytarib bo‘lmaydi.", 409)
        stage.status, stage.activated_at, stage.due_at = StageStatus.WAITING, None, None
        stage.save()
        target = case.stages.get(number=number - 1)
        _open(target, StageStatus.RETURNED, now)
        target.return_reason, target.decided_by, target.decided_at = reason, user, now
        target.save()
        case.current_stage = target.number
        case.save(update_fields=["current_stage"])
    event = _event(case, target.number, StageEvent.RETURN, user, reason, from_stage=number)
    _notify("returned", executor_users(case, target.number), case, target, f"ev{event.pk}:returned", reason)
    return case


@transaction.atomic
def reject(user: User, case_id: int, *, reason: str) -> Case:
    case = _lock(case_id)
    if case.status != CaseStatus.IN_PROGRESS:
        raise ChainError(MSG_CLOSED, 409)
    stage = case.stages.get(number=case.current_stage)
    if not can_reject(user, case, stage):
        raise ChainError(MSG_FORBIDDEN, 403)
    if not reason.strip():
        raise ChainError("Sababni yozing.", 400, errors={"reason": ["Bu maydon to‘ldirilishi shart."]})
    case.status, case.reject_reason = CaseStatus.REJECTED, reason
    case.save(update_fields=["status", "reject_reason"])
    event = _event(case, stage.number, StageEvent.REJECT, user, reason)
    users = _role_users(Role.ADMIN) + ([case.created_by] if case.created_by else [])
    _notify("rejected", users, case, stage, f"ev{event.pk}:rejected", reason)
    return case


@transaction.atomic
def owner_account(user: User, case_id: int, *, link_user_id: int | None = None) -> dict:
    """Create (or link) the OWNER account of the case. Returns {username, temp_password}."""
    case = _lock(case_id)
    if link_user_id:
        owner = User.objects.alive().filter(pk=link_user_id, role=Role.OWNER).first()
        if owner is None:
            raise ChainError("Xonadon egasi (OWNER) topilmadi.", 404)
        password = None
    elif case.owner_user_id:
        owner, password = case.owner_user, make_temp_password()
        owner.set_password(password)
        owner.must_change_password = True
        owner.token_version += 1
        owner.save()
    else:
        base = case.code.lower().replace("-", "")
        username, i = base, 1
        while User.objects.filter(username=username).exists():
            i += 1
            username = f"{base}_{i}"
        password = make_temp_password()
        owner = User(username=username, role=Role.OWNER, full_name=case.owner_full_name, phone=case.owner_phone,
                     must_change_password=True)
        owner.set_password(password)
        owner.save()
        log(user, "user.create", owner, after={"username": owner.username, "role": owner.role})
    case.owner_user = owner
    case.save(update_fields=["owner_user"])
    log(user, "case.owner_account", case, after={"owner_user": owner.pk})
    return {"username": owner.username, "temp_password": password, "user_id": owner.pk}


# --- Next partiya (batch) -----------------------------------------------------------------------

#: A new partiya may start from HR (2) at the earliest and from chick delivery (7) at the latest.
NEXT_CYCLE_CHOICES = tuple(range(2, chain.DELIVERY_STAGE + 1))
CARRIED_OVER = "carried_over"


def default_next_cycle_stage() -> int:
    from django.conf import settings

    value = int(getattr(settings, "NEXT_CYCLE_START_STAGE", chain.INCUBATION_STAGE))
    return value if value in NEXT_CYCLE_CHOICES else chain.INCUBATION_STAGE


def household_cycles(case: Case):
    """All partiyas of the household of `case`, oldest first."""
    root_id = case.root_id or case.pk
    return Case.objects.filter(Q(pk=root_id) | Q(root_id=root_id)).order_by("cycle", "pk")


def can_start_next_cycle(user: User, case: Case) -> bool:
    """Admin starts the next partiya of a finished household, only from its latest partiya."""
    if user.role not in (Role.ADMIN, Role.SUPERADMIN) or case.status != CaseStatus.DONE:
        return False
    return not household_cycles(case).filter(cycle__gt=case.cycle).exists()


def is_carried_over(stage: CaseStage) -> bool:
    return bool((stage.data or {}).get(CARRIED_OVER))


@transaction.atomic
def start_next_cycle(user: User, case_id: int, *, start_stage: int | None = None) -> Case:
    """New case for the next partiya: household data, owner and vet are copied; stages before `start_stage`
    (paperwork, construction, ...) are carried over as DONE without work, and `start_stage` opens now."""
    previous = _lock(case_id)
    start = default_next_cycle_stage() if start_stage is None else start_stage
    if start not in NEXT_CYCLE_CHOICES:
        raise ChainError("Boshlanish bosqichi noto‘g‘ri.", 400,
                         errors={"start_stage": [f"{NEXT_CYCLE_CHOICES[0]}–{NEXT_CYCLE_CHOICES[-1]} oralig‘ida."]})
    if previous.status != CaseStatus.DONE:
        raise ChainError("Yangi partiya faqat oldingi partiya yakunlangandan keyin boshlanadi.", 409)
    if not can_start_next_cycle(user, previous):
        if user.role not in (Role.ADMIN, Role.SUPERADMIN):
            raise ChainError(MSG_FORBIDDEN, 403)
        raise ChainError("Bu xonadonning keyingi partiyasi allaqachon boshlangan.", 409)

    now = timezone.now()
    root = previous.root or previous
    cycle = previous.cycle + 1
    case = Case.objects.create(
        **{key: getattr(previous, key) for key in HOUSEHOLD_FIELDS},
        package=previous.package, owner_user=previous.owner_user, vet_user=previous.vet_user,
        cycle=cycle, root=root, created_by=user, current_stage=start,
    )
    case.code = f"{root.code}-{cycle}"
    case.save(update_fields=["code"])

    first_data = next((st.data for st in previous.stages.all() if st.number == 1), None) or {}
    note = f"{previous.code} partiyasidan o‘tkazildi"
    stages = []
    for definition in chain.STAGES:
        stage = CaseStage(case=case, number=definition.number, key=definition.key)
        if definition.number < start:
            stage.status, stage.activated_at, stage.note = StageStatus.DONE, now, note
            stage.data = {CARRIED_OVER: True, "from_case": previous.code}
            if definition.number == 1:
                stage.data = {**{k: v for k, v in first_data.items() if k != "finance"}, **stage.data}
        elif definition.number == start:
            _open(stage, StageStatus.ACTIVE, now)
        stages.append(stage)
    CaseStage.objects.bulk_create(stages)

    event = _event(case, start, StageEvent.CREATE, user,
                   f"{cycle}-partiya ({previous.code} dan) {start}-bosqichdan boshlandi",
                   cycle=cycle, previous=previous.code, start_stage=start)
    _notify("active", executor_users(case, start), case, case.stages.get(number=start), f"ev{event.pk}:active")
    return case


def _stage_def(number: int) -> chain.Stage:
    if number not in chain.BY_NUMBER:
        raise ChainError("Bosqich topilmadi.", 404)
    return chain.get(number)


def _current(case: Case, number: int, allowed) -> CaseStage:
    if case.status != CaseStatus.IN_PROGRESS:
        raise ChainError(MSG_CLOSED, 409)
    stage = case.stages.get(number=number)
    if case.current_stage != number or stage.status not in allowed:
        raise ChainError(MSG_NOT_CURRENT, 409)
    return stage


# --- Scheduler ----------------------------------------------------------------------------------

def send_overdue_reminders(now=None) -> int:
    """Once a day per stage: notify the executor role and ADMIN about an overdue stage."""
    now = now or timezone.now()
    today = timezone.localdate(now)
    overdue = (
        CaseStage.objects.filter(status__in=OPEN, case__status=CaseStatus.IN_PROGRESS, case__deleted_at__isnull=True,
                                 due_at__lt=now)
        .exclude(overdue_notified_on=today)
        .select_related("case", "case__owner_user")
    )
    sent = 0
    for stage in overdue:
        with transaction.atomic():
            claimed = CaseStage.objects.filter(pk=stage.pk).exclude(overdue_notified_on=today).update(
                overdue_notified_on=today)
            if claimed:
                users = executor_users(stage.case, stage.number) + _role_users(Role.ADMIN)
                _notify("overdue", users, stage.case, stage, f"overdue:{stage.pk}:{today.isoformat()}")
                sent += 1
    return sent

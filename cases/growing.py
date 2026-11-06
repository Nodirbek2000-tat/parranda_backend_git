"""v2.1: owner requests, vet assignment, vet visits and the growing-period scheduler jobs.

Same rules as the chain (cases/services.py): every write runs in one transaction with the row locked
(plain `select_for_update()` without select_related), is audited, and queues notifications on commit with
a unique event key. Stock changes only through stock.services.
"""

from datetime import timedelta
from decimal import Decimal

from django.db import IntegrityError, transaction
from django.db.models import Q
from django.utils import timezone

from accounts.models import Role, User
from audit.models import log
from notify import services as notify
from notify.models import Notification
from stock import services as stock
from stock.models import Category, Product, StockMove

from . import stages as chain
from .models import (Case, CaseStage, CaseStatus, Photo, Request, RequestKind, RequestStatus, StageStatus, VetVisit,
                     VisitDrug, VisitKind, VisitStatus)
from .services import MSG_FORBIDDEN, ChainError, _role_users, in_growing_period

MSG_NOT_GROWING = "So‘rovlar faqat parvarish davrida (10-bosqich) qabul qilinadi."
MSG_WRONG_STATUS = "So‘rov bu holatda emas."
MSG_VISIT_STATUS = "Tashrif bu holatda emas."
MSG_NO_VET = "Xonadonga veterinar biriktirilmagan. Avval veterinarni tanlang."
MSG_SHORT = "Omborda yetarli mahsulot yo‘q."
ROUTINE_CANCEL_REASON = "Parvarish davri yakunlandi"


# --- Visibility and flags -----------------------------------------------------------------------

def visible_requests(user: User):
    qs = Request.objects.filter(case__deleted_at__isnull=True)
    if user.is_admin:
        return qs
    if user.role == Role.OWNER:
        return qs.filter(case__owner_user=user)
    if user.role == Role.LOGISTICS:
        return qs.filter(kind=RequestKind.FEED)
    if user.role == Role.VET:
        return qs.filter(kind=RequestKind.VET).filter(Q(case__vet_user=user) | Q(case__vet_user__isnull=True))
    return qs.none()


def visible_visits(user: User):
    qs = VetVisit.objects.filter(case__deleted_at__isnull=True)
    if user.is_admin:
        return qs
    if user.role == Role.VET:
        return qs.filter(vet=user)
    if user.role == Role.OWNER:
        return qs.filter(case__owner_user=user)
    return qs.none()


def request_can(user: User, req: Request, growing: bool | None = None) -> dict:
    case = req.case
    if growing is None:
        growing = in_growing_period(case)
    logistics_step = req.status == RequestStatus.PENDING_LOGISTICS and user.role == Role.LOGISTICS
    admin_step = req.status == RequestStatus.PENDING_ADMIN and user.is_admin
    is_owner = user.role == Role.OWNER and case.owner_user_id == user.pk
    return {
        "approve": (logistics_step or admin_step) and growing,
        "reject": logistics_step or admin_step,
        "deliver": req.kind == RequestKind.FEED and req.status == RequestStatus.APPROVED and user.role == Role.LOGISTICS,
        "confirm": req.status == RequestStatus.DELIVERED and (is_owner or user.is_admin),
    }


def visit_can(user: User, visit: VetVisit) -> dict:
    planned = visit.status == VisitStatus.PLANNED
    return {
        "complete": planned and user.role == Role.VET and visit.vet_id == user.pk,
        "cancel": planned and user.is_admin,
    }


def growing_info(case: Case, stage: CaseStage | None = None) -> dict | None:
    """{day, total_days, days_left, started_at, ends_at, is_over} while stage 10 is ACTIVE, else None."""
    if stage is None or stage.number != chain.GROWING_STAGE:
        stage = case.stages.filter(number=chain.GROWING_STAGE).first()
    if stage is None or not in_growing_period(case, stage) or stage.activated_at is None:
        return None
    start = timezone.localdate(stage.activated_at)
    ends_at = stage.due_at or stage.activated_at + timedelta(days=chain.growing_days())
    end = timezone.localdate(ends_at)
    total = max(1, (end - start).days)
    today = timezone.localdate()
    return {
        "day": max(1, min((today - start).days + 1, total)),
        "total_days": total,
        "days_left": max(0, (end - today).days),
        "started_at": stage.activated_at,
        "ends_at": ends_at,
        "is_over": today >= end,
    }


# --- Helpers ------------------------------------------------------------------------------------

def _lock_case(case_id: int) -> Case:
    try:
        return Case.objects.select_for_update().get(pk=case_id)
    except Case.DoesNotExist:
        raise ChainError("Xonadon topilmadi.", 404) from None


def _lock_request(pk: int) -> Request:
    try:
        return Request.objects.select_for_update(of=("self",)).get(pk=pk, case__deleted_at__isnull=True)
    except Request.DoesNotExist:
        raise ChainError("So‘rov topilmadi.", 404) from None


def _lock_visit(pk: int) -> VetVisit:
    try:
        return VetVisit.objects.select_for_update(of=("self",)).get(pk=pk, case__deleted_at__isnull=True)
    except VetVisit.DoesNotExist:
        raise ChainError("Tashrif topilmadi.", 404) from None


def _attach_photos(ids, user: User, case: Case, *, request=None, visit=None) -> list[int]:
    """Attach the caller's own fresh uploads to a request or a visit (and its case, for access checks)."""
    ids = list(dict.fromkeys(int(i) for i in ids or []))
    if not ids:
        return []
    rows = list(Photo.objects.select_for_update(of=("self",)).filter(pk__in=ids))
    ok = {row.pk for row in rows if row.case_id is None and row.uploaded_by_id == user.pk}
    missing = [i for i in ids if i not in ok]
    if missing:
        msg = f"Rasm topilmadi yoki boshqa joyga biriktirilgan: {missing}"
        raise ChainError(msg, 400, errors={"photo_ids": [msg]})
    Photo.objects.filter(pk__in=ids).update(case=case, request=request, visit=visit)
    return ids


def _alive(user):
    return user is not None and user.is_active and user.deleted_at is None


def _owner_users(req: Request) -> list[User]:
    owner = req.case.owner_user
    if _alive(owner):
        return [owner]
    return [req.created_by] if _alive(req.created_by) else []


def _vet_users(case: Case) -> list[User]:
    return [case.vet_user] if _alive(case.vet_user) else _role_users(Role.VET)


def _qty(value) -> str:
    value = Decimal(value).normalize()
    return format(value, "f")


def _notify_request(kind: str, users, req: Request, event: str, note: str = "") -> None:
    rows = [
        ("So‘rov", f"{req.get_kind_display()} #{req.pk}"),
        ("Miqdor", f"{_qty(req.qty_kg)} kg" if req.qty_kg else ""),
        ("Sabab", req.text),
        ("Holat", req.get_status_display()),
        ("Izoh", note),
    ]
    notify.queue(users, text=notify.compose_rows(kind, req.case, rows, op_id=f"{req.case.code}-S{req.pk}"),
                 button_path=f"/requests/{req.pk}", event_key=f"req{req.pk}:{event}")


def _notify_visit(kind: str, users, visit: VetVisit, event: str, note: str = "") -> None:
    rows = [
        ("Tashrif", f"{visit.get_kind_display()}" + (f" ({visit.week}-hafta)" if visit.week else "")),
        ("Sana", visit.planned_date.strftime("%d.%m.%Y")),
        ("Veterinar", visit.vet.full_name if visit.vet else "biriktirilmagan"),
        ("Izoh", note),
    ]
    notify.queue(users, text=notify.compose_rows(kind, visit.case, rows, op_id=f"{visit.case.code}-V{visit.pk}"),
                 button_path=f"/visits/{visit.pk}", event_key=f"visit{visit.pk}:{event}")


def _require_reason(reason: str) -> str:
    reason = (reason or "").strip()
    if not reason:
        raise ChainError("Sababni yozing.", 400, errors={"reason": ["Bu maydon to‘ldirilishi shart."]})
    return reason


def _shortage(exc: stock.StockShortage) -> ChainError:
    return ChainError(MSG_SHORT, 409, missing=exc.missing)


# --- Vet assignment -----------------------------------------------------------------------------

@transaction.atomic
def assign_vet(user: User, case_id: int, vet_id: int | None) -> Case:
    if not user.is_admin:
        raise ChainError(MSG_FORBIDDEN, 403)
    vet = None
    if vet_id is not None:
        vet = User.objects.active_with_role(Role.VET).filter(pk=vet_id).first()
        if vet is None:
            raise ChainError("Veterinar topilmadi.", 400, errors={"vet_id": ["Faol veterinar (VET) tanlang."]})
    case = _lock_case(case_id)
    before = case.vet_user_id
    case.vet_user = vet
    case.save(update_fields=["vet_user"])
    if vet is not None:
        VetVisit.objects.filter(case=case, status=VisitStatus.PLANNED).exclude(vet=vet).update(vet=vet)
    entry = log(user, "case.assign_vet", case, before={"vet_user": before}, after={"vet_user": vet_id})
    if vet is not None and vet.pk != before:
        rows = [("Veterinar", vet.full_name), ("Bosqich", f"{case.current_stage}. {chain.get(case.current_stage).title}")]
        notify.queue([vet], text=notify.compose_rows("vet_assigned", case, rows, op_id=f"{case.code}-VET"),
                     button_path=f"/cases/{case.pk}", event_key=f"assignvet:{case.pk}:{entry.pk}")
    return case


# --- Requests -----------------------------------------------------------------------------------

@transaction.atomic
def create_request(user: User, case_id: int, *, kind: str, text: str, qty_kg=None, photo_ids=()) -> Request:
    case = _lock_case(case_id)
    if not (user.role == Role.OWNER and case.owner_user_id == user.pk):
        raise ChainError("So‘rovni faqat xonadon egasi yuboradi.", 403)
    if not in_growing_period(case):
        raise ChainError(MSG_NOT_GROWING, 409)
    status = RequestStatus.PENDING_LOGISTICS if kind == RequestKind.FEED else RequestStatus.PENDING_ADMIN
    req = Request.objects.create(case=case, kind=kind, text=text.strip(), qty_kg=qty_kg, status=status,
                                 created_by=user)
    _attach_photos(photo_ids, user, case, request=req)
    log(user, "request.create", req, after={"case": case.pk, "kind": kind, "qty_kg": str(qty_kg or ""),
                                           "text": req.text})
    users = _role_users(Role.LOGISTICS) if kind == RequestKind.FEED else _role_users(Role.ADMIN) + _vet_users(case)
    _notify_request("request_new", users, req, "created")
    return req


@transaction.atomic
def approve_request(user: User, pk: int, *, note: str = "") -> Request:
    req = _lock_request(pk)
    case = req.case
    now = timezone.now()
    if req.status == RequestStatus.PENDING_LOGISTICS:
        if user.role != Role.LOGISTICS:
            raise ChainError(MSG_FORBIDDEN, 403)
        if not in_growing_period(case):
            raise ChainError(MSG_NOT_GROWING, 409)
        req.status, req.logistics_by, req.logistics_at = RequestStatus.PENDING_ADMIN, user, now
        req.save()
        _notify_request("request_review", _role_users(Role.ADMIN), req, "logistics_ok", note)
    elif req.status == RequestStatus.PENDING_ADMIN:
        if not user.is_admin:
            raise ChainError(MSG_FORBIDDEN, 403)
        if not in_growing_period(case):
            raise ChainError(MSG_NOT_GROWING, 409)
        req.status, req.admin_by, req.admin_at = RequestStatus.APPROVED, user, now
        if req.kind == RequestKind.VET:
            if not _alive(case.vet_user) or case.vet_user.role != Role.VET:
                raise ChainError(MSG_NO_VET, 400)
            req.save()
            visit = VetVisit.objects.create(case=case, vet=case.vet_user, kind=VisitKind.REQUEST, request=req,
                                            planned_date=timezone.localdate(now))
            log(user, "visit.create", visit, after={"case": case.pk, "request": req.pk, "vet": case.vet_user_id})
            _notify_visit("visit_planned", [case.vet_user], visit, "planned", req.text)
        else:
            req.save()
            _notify_request("request_deliver", _role_users(Role.LOGISTICS), req, "admin_ok", note)
    else:
        raise ChainError(MSG_WRONG_STATUS, 409)
    log(user, "request.approve", req, after={"status": req.status, "note": note})
    return req


@transaction.atomic
def reject_request(user: User, pk: int, *, reason: str) -> Request:
    req = _lock_request(pk)
    if req.status == RequestStatus.PENDING_LOGISTICS:
        allowed = user.role == Role.LOGISTICS
    elif req.status == RequestStatus.PENDING_ADMIN:
        allowed = user.is_admin
    else:
        raise ChainError(MSG_WRONG_STATUS, 409)
    if not allowed:
        raise ChainError(MSG_FORBIDDEN, 403)
    reason = _require_reason(reason)
    _reject(user, req, reason)
    return req


def _reject(user: User, req: Request, reason: str) -> None:
    req.status, req.rejected_by, req.rejected_at, req.reject_reason = (
        RequestStatus.REJECTED, user, timezone.now(), reason)
    req.save()
    log(user, "request.reject", req, after={"reason": reason})
    _notify_request("request_rejected", _owner_users(req), req, "rejected", reason)


@transaction.atomic
def deliver_request(user: User, pk: int, *, product_id: int, qty: Decimal, photo_ids, note: str = "") -> Request:
    req = _lock_request(pk)
    if req.kind != RequestKind.FEED or req.status != RequestStatus.APPROVED:
        raise ChainError(MSG_WRONG_STATUS, 409)
    if user.role != Role.LOGISTICS:
        raise ChainError(MSG_FORBIDDEN, 403)
    product = Product.objects.filter(pk=product_id, category=Category.YEM).first()
    if product is None:
        raise ChainError("Ma’lumotlar noto‘g‘ri.", 400, errors={"product_id": ["Yem (yem turkumi) mahsulotini tanlang."]})
    if not photo_ids:
        raise ChainError("Kamida bitta jonli rasm kerak.", 400, errors={"photo_ids": ["Kamida bitta rasm yuklang."]})
    case = req.case
    try:
        stock.issue_items(case, [(product.pk, qty)], user=user, note=f"{case.code}: yem so‘rovi #{req.pk}",
                          purpose=StockMove.REQUEST)
    except stock.StockShortage as exc:
        raise _shortage(exc) from None
    ids = _attach_photos(photo_ids, user, case, request=req)
    req.status, req.delivered_by, req.delivered_at = RequestStatus.DELIVERED, user, timezone.now()
    req.delivered_product, req.delivered_qty, req.delivery_note, req.delivery_photo_ids = product, qty, note, ids
    req.save()
    log(user, "request.deliver", req, after={"product": product.pk, "qty": str(qty), "note": note})
    _notify_request("request_delivered", _owner_users(req), req, "delivered",
                    f"{product.name}: {_qty(qty)} {product.unit}")
    return req


@transaction.atomic
def confirm_request(user: User, pk: int) -> Request:
    req = _lock_request(pk)
    if req.status != RequestStatus.DELIVERED:
        raise ChainError(MSG_WRONG_STATUS, 409)
    if not ((user.role == Role.OWNER and req.case.owner_user_id == user.pk) or user.is_admin):
        raise ChainError(MSG_FORBIDDEN, 403)
    req.status, req.done_by, req.done_at = RequestStatus.DONE, user, timezone.now()
    req.save()
    log(user, "request.confirm", req)
    return req


# --- Visits -------------------------------------------------------------------------------------

@transaction.atomic
def complete_visit(user: User, pk: int, *, chicks_count: int, health: str, note: str = "", photo_ids=(),
                   drugs=()) -> VetVisit:
    visit = _lock_visit(pk)
    if visit.status != VisitStatus.PLANNED:
        raise ChainError(MSG_VISIT_STATUS, 409)
    if not (user.role == Role.VET and visit.vet_id == user.pk):
        raise ChainError("Tashrifni faqat biriktirilgan veterinar yakunlaydi.", 403)
    if not photo_ids:
        raise ChainError("Kamida bitta jonli rasm kerak.", 400, errors={"photo_ids": ["Kamida bitta rasm yuklang."]})
    items = [(int(d["product_id"]), Decimal(d["qty"])) for d in drugs or []]
    ids = {pid for pid, _ in items}
    found = set(Product.objects.filter(pk__in=ids, category=Category.DORI).values_list("pk", flat=True))
    if ids - found:
        raise ChainError("Ma’lumotlar noto‘g‘ri.", 400, errors={"drugs": [
            f"Veterinariya dorisi topilmadi: {sorted(ids - found)}"]})
    case = visit.case
    if items:
        try:
            stock.issue_items(case, items, user=user, note=f"{case.code}: veterinar tashrifi #{visit.pk}",
                              purpose=StockMove.VISIT)
        except stock.StockShortage as exc:
            raise _shortage(exc) from None
        VisitDrug.objects.bulk_create([VisitDrug(visit=visit, product_id=pid, qty=qty) for pid, qty in items])
    _attach_photos(photo_ids, user, case, visit=visit)
    visit.status, visit.done_at = VisitStatus.DONE, timezone.now()
    visit.chicks_count, visit.health, visit.note = chicks_count, health, note
    visit.save()
    log(user, "visit.complete", visit, after={"chicks_count": chicks_count, "health": health, "note": note,
                                             "drugs": [[pid, str(q)] for pid, q in items]})
    if visit.request_id:
        req = Request.objects.select_for_update().get(pk=visit.request_id)
        if req.status == RequestStatus.APPROVED:
            req.status, req.done_by, req.done_at = RequestStatus.DONE, user, visit.done_at
            req.save()
            log(user, "request.done", req, after={"visit": visit.pk})
    return visit


@transaction.atomic
def cancel_visit(user: User, pk: int, *, reason: str) -> VetVisit:
    visit = _lock_visit(pk)
    if visit.status != VisitStatus.PLANNED:
        raise ChainError(MSG_VISIT_STATUS, 409)
    if not user.is_admin:
        raise ChainError(MSG_FORBIDDEN, 403)
    reason = _require_reason(reason)
    visit.status, visit.cancel_reason = VisitStatus.CANCELLED, reason
    visit.cancelled_by, visit.cancelled_at = user, timezone.now()
    visit.save()
    log(user, "visit.cancel", visit, after={"reason": reason})
    if visit.vet_id and _alive(visit.vet):
        _notify_visit("visit_cancelled", [visit.vet], visit, "cancelled", reason)
    if visit.request_id:
        req = Request.objects.select_for_update().get(pk=visit.request_id)
        if req.status == RequestStatus.APPROVED:
            _reject(user, req, f"Tashrif bekor qilindi: {reason}")
    return visit


def cancel_routine_visits(case: Case, user: User, now) -> int:
    """Stage 10 is done (sent to the slaughterhouse): planned weekly visits are not needed any more."""
    qs = VetVisit.objects.filter(case=case, kind=VisitKind.ROUTINE, status=VisitStatus.PLANNED)
    ids = list(qs.values_list("pk", flat=True))
    if ids:
        VetVisit.objects.filter(pk__in=ids).update(status=VisitStatus.CANCELLED, cancel_reason=ROUTINE_CANCEL_REASON,
                                                   cancelled_by=user, cancelled_at=now)
        log(user, "visit.cancel_routine", case, after={"visits": ids})
    return len(ids)


# --- Scheduler (send_reminders, every 15 min, idempotent) ---------------------------------------

def _growing_stages():
    return (CaseStage.objects.filter(
        number=chain.GROWING_STAGE, status=StageStatus.ACTIVE, activated_at__isnull=False,
        case__status=CaseStatus.IN_PROGRESS, case__current_stage=chain.GROWING_STAGE, case__deleted_at__isnull=True)
        .select_related("case", "case__vet_user").order_by("case_id"))


def plan_weekly_visits(today=None) -> int:
    """Create the ROUTINE visit of the latest week (day 7, 14, ... ≤ GROWING_DAYS) that is due within 1 day."""
    today = today or timezone.localdate()
    created = 0
    for stage in _growing_stages():
        case = stage.case
        if not _alive(case.vet_user):
            continue
        start = timezone.localdate(stage.activated_at)
        total = (timezone.localdate(stage.due_at) - start).days if stage.due_at else chain.growing_days()
        week = min(((today + timedelta(days=1)) - start).days // 7, total // 7)
        if week < 1 or VetVisit.objects.filter(case=case, kind=VisitKind.ROUTINE, week=week).exists():
            continue
        try:
            with transaction.atomic():
                visit = VetVisit.objects.create(case=case, vet=case.vet_user, kind=VisitKind.ROUTINE, week=week,
                                                planned_date=start + timedelta(days=7 * week))
                log(None, "visit.create", visit, after={"case": case.pk, "week": week, "vet": case.vet_user_id})
                _notify_visit("visit_planned", [case.vet_user], visit, "planned")
        except IntegrityError:  # created concurrently
            continue
        created += 1
    return created


def visit_reminders(today=None) -> int:
    """Vet: on the planned day (once); vet + ADMIN: every day while a PLANNED visit is overdue."""
    today = today or timezone.localdate()
    live = VetVisit.objects.filter(status=VisitStatus.PLANNED, case__status=CaseStatus.IN_PROGRESS,
                                   case__deleted_at__isnull=True)
    sent = 0
    for visit in live.filter(planned_date=today).exclude(due_notified_on=today).select_related("case", "vet"):
        with transaction.atomic():
            if VetVisit.objects.filter(pk=visit.pk).exclude(due_notified_on=today).update(due_notified_on=today):
                users = [visit.vet] if _alive(visit.vet) else _role_users(Role.ADMIN)
                _notify_visit("visit_due", users, visit, f"due:{today.isoformat()}")
                sent += 1
    for visit in live.filter(planned_date__lt=today).exclude(overdue_notified_on=today).select_related("case", "vet"):
        with transaction.atomic():
            if VetVisit.objects.filter(pk=visit.pk).exclude(overdue_notified_on=today).update(
                    overdue_notified_on=today):
                users = ([visit.vet] if _alive(visit.vet) else []) + _role_users(Role.ADMIN)
                _notify_visit("visit_overdue", users, visit, f"overdue:{today.isoformat()}")
                sent += 1
    return sent


def growing_alerts(today=None) -> int:
    """ADMIN: two days before the growing period ends, and again on the end day (one notification each)."""
    today = today or timezone.localdate()
    sent = 0
    for stage in _growing_stages():
        case = stage.case
        start = timezone.localdate(stage.activated_at)
        end = timezone.localdate(stage.due_at) if stage.due_at else start + timedelta(days=chain.growing_days())
        if today >= end:
            tag = "end"
        elif today >= end - timedelta(days=2):
            tag = "soon"
        else:
            continue
        key = f"growing:{case.pk}:{start.isoformat()}:{tag}"
        if Notification.objects.filter(event_key__startswith=key + ":").exists():
            continue
        total = (end - start).days
        rows = [("Bosqich", f"{chain.GROWING_STAGE}. {chain.get(chain.GROWING_STAGE).title}"),
                ("Kun", f"{min((today - start).days + 1, total)} / {total}"),
                ("Tugash sanasi", end.strftime("%d.%m.%Y"))]
        lead = f"{case.code}: {total} kunlik parvarish yakuniga yetmoqda — qushxonaga yuborish vaqti"
        with transaction.atomic():
            notify.queue(_role_users(Role.ADMIN),
                         text=notify.compose_rows("growing_alert", case, rows, op_id=f"{case.code}-10", lead=lead),
                         button_path=f"/cases/{case.pk}", event_key=key)
        sent += 1
    return sent

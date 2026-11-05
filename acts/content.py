"""Completed work of each role for an act (API_CONTRACT_V4 §6 "Role act content").

Only real, finished work is counted: cancelled rows, returned deliveries and ``is_test`` builder rows are left
out. Every builder returns :class:`RoleContent` (summary + table sections) built from a fixed number of queries.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from typing import Any

from django.db.models import Q, QuerySet, Sum

from accounts.models import User
from audit.models import AuditAction, AuditLog, AuditModule, AuditObjectType
from builder.access import scope_households
from builder.models import Blueprint, BlueprintKind, ProcessRun, ProcessRunStatus, TaskInstance, TaskStatus
from common.formatting import fmt_date, fmt_number
from common.periods import day_range
from common.roles import Role
from farm.models import DailyReport, Household, MovementType, PoultryMovement
from logistics.models import LogisticsTask
from logistics.models import TaskStatus as LogisticsStatus
from orders.models import Order, OrderStatus
from vet.models import DiseaseRecord, RequestStatus, Vaccination, VetRequest, VetStatus, VetVisit
from warehouse.models import (
    TASK_REFERENCE,
    InventoryCount,
    InventoryCountStatus,
    StockTransaction,
    StockTransactionType,
    returned_task_out,
)

DASH = "—"
DELIVERED_STATUSES = (LogisticsStatus.DELIVERED, LogisticsStatus.CONFIRMED)
APPROVED_ORDER_STATUSES = (
    OrderStatus.APPROVED, OrderStatus.IN_DELIVERY, OrderStatus.DELIVERED, OrderStatus.CONFIRMED,
)


@dataclass
class Window:
    """What an act covers: period, optional household / user, and the caller whose scope applies."""

    date_from: date
    date_to: date
    household: Household | None = None
    user: User | None = None
    viewer: User | None = None

    @property
    def start(self):
        return day_range(self.date_from, self.date_to)[0]

    @property
    def end(self):
        return day_range(self.date_from, self.date_to)[1]

    def moments(self, field_name: str) -> Q:
        """``field`` (datetime) inside the period."""
        return Q(**{f"{field_name}__gte": self.start, f"{field_name}__lt": self.end})

    def days(self, field_name: str) -> Q:
        """``field`` (date) inside the period."""
        return Q(**{f"{field_name}__gte": self.date_from, f"{field_name}__lte": self.date_to})

    def households(self, qs: QuerySet, field_name: str = "household") -> QuerySet:
        """Household filter + the viewer's territorial scope (V4 §2)."""
        if self.household is not None:
            qs = qs.filter(**{field_name: self.household})
        return scope_households(self.viewer, qs, field_name)


@dataclass
class Section:
    title: str
    columns: list[str]
    rows: list[list[str]]

    def as_dict(self) -> dict[str, Any]:
        return {"title": self.title, "columns": self.columns, "rows": self.rows}


@dataclass
class RoleContent:
    summary: list[tuple[str, str]] = field(default_factory=list)
    sections: list[Section] = field(default_factory=list)


# --- Formatting --------------------------------------------------------------------------------


def text(value: Any) -> str:
    if value is None or value == "":
        return DASH
    if isinstance(value, bool):
        return "Ha" if value else "Yo‘q"
    if isinstance(value, (int, float, Decimal)):
        return fmt_number(value)
    if isinstance(value, date):
        return fmt_date(value)
    return " ".join(str(value).split())


def fmt_moment(value) -> str:
    from django.utils import timezone

    return timezone.localtime(value).strftime("%d.%m.%Y %H:%M") if value else DASH


def household_label(household: Household | None) -> str:
    return f"{household.code} {household.name}" if household is not None else DASH


def name_of(user: User | None) -> str:
    return user.full_name if user is not None else DASH


def _sum(qs: QuerySet, field_name: str) -> Decimal:
    return qs.aggregate(total=Sum(field_name))["total"] or Decimal(0)


# --- ADMIN -------------------------------------------------------------------------------------


def admin_content(w: Window) -> RoleContent:
    households = scope_households(w.viewer, Household.objects.filter(w.moments("created_at")))
    if w.household is not None:
        households = households.filter(pk=w.household.pk)
    if w.user is not None:
        created_ids = AuditLog.objects.filter(
            module=AuditModule.HOUSEHOLDS, action=AuditAction.CREATE, object_type=AuditObjectType.HOUSEHOLD,
            actor=w.user,
        ).values("object_id")
        households = households.filter(pk__in=created_ids)
    households = list(households.select_related("owner").order_by("created_at", "id"))

    orders = w.households(Order.objects.filter(status__in=APPROVED_ORDER_STATUSES).filter(w.moments("decided_at")))
    if w.user is not None:
        orders = orders.filter(decided_by=w.user)
    orders = list(orders.select_related("household", "decided_by").order_by("decided_at", "id"))

    runs = w.households(
        ProcessRun.objects.filter(is_test=False).exclude(status=ProcessRunStatus.CANCELLED)
        .filter(w.moments("started_at") | w.moments("finished_at"))
    )
    if w.user is not None:
        runs = runs.filter(started_by=w.user)
    runs = list(runs.select_related("process", "household").order_by("started_at", "id"))
    started = [run for run in runs if run.started_at and w.start <= run.started_at < w.end]
    finished = [
        run for run in runs
        if run.status == ProcessRunStatus.DONE and run.finished_at and w.start <= run.finished_at < w.end
    ]

    return RoleContent(
        summary=[
            ("Ro‘yxatga olingan xonadonlar", text(len(households))),
            ("Tasdiqlangan buyurtmalar", text(len(orders))),
            ("Boshlangan jarayonlar", text(len(started))),
            ("Yakunlangan jarayonlar", text(len(finished))),
        ],
        sections=[
            Section(
                "Ro‘yxatga olingan xonadonlar",
                ["Sana", "Xonadon", "Egasi", "Tuman", "Manzil"],
                [
                    [fmt_date(h.created_at), household_label(h), name_of(h.owner), text(h.district), text(h.address)]
                    for h in households
                ],
            ),
            Section(
                "Tasdiqlangan buyurtmalar",
                ["Sana", "Kod", "Xonadon", "Turi", "Holati", "Tasdiqladi"],
                [
                    [fmt_date(o.decided_at), o.code, household_label(o.household), o.get_kind_display(),
                     o.get_status_display(), name_of(o.decided_by)]
                    for o in orders
                ],
            ),
            Section(
                "Jarayonlar",
                ["Boshlangan", "Kod", "Jarayon", "Xonadon", "Holati", "Yakunlangan"],
                [
                    [fmt_date(r.started_at), r.code, r.process.name, household_label(r.household),
                     r.get_status_display(), fmt_date(r.finished_at) if r.finished_at else DASH]
                    for r in runs
                ],
            ),
        ],
    )


# --- WAREHOUSE ---------------------------------------------------------------------------------


def warehouse_content(w: Window) -> RoleContent:
    stock = StockTransaction.objects.select_related("product", "household", "created_by").filter(w.moments("created_at"))
    if w.user is not None:
        stock = stock.filter(created_by=w.user)
    receipts = stock.filter(type=StockTransactionType.IN).exclude(reference_type=TASK_REFERENCE)
    if w.household is not None:
        receipts = receipts.none()  # receipts belong to the warehouse, not to a household
    issues = w.households(stock.filter(type=StockTransactionType.OUT).exclude(returned_task_out()))
    receipts = list(receipts.order_by("created_at", "id"))
    issues = list(issues.order_by("created_at", "id"))

    movements = w.households(
        PoultryMovement.objects.filter(type=MovementType.DISTRIBUTE).filter(w.days("date"))
        .select_related("batch", "household")
    )
    if w.user is not None:
        movements = movements.filter(created_by=w.user)
    movements = list(movements.order_by("date", "id"))

    counts = InventoryCount.objects.filter(status=InventoryCountStatus.APPROVED).filter(w.moments("approved_at"))
    if w.user is not None:
        counts = counts.filter(Q(created_by=w.user) | Q(approved_by=w.user))
    if w.household is not None:
        counts = counts.none()
    counts = list(counts.select_related("approved_by").prefetch_related("lines").order_by("approved_at", "id"))

    def qty(row: StockTransaction) -> str:
        return f"{text(row.quantity)} {row.product.unit}"

    return RoleContent(
        summary=[
            ("Kirim operatsiyalari", text(len(receipts))),
            ("Chiqim operatsiyalari", text(len(issues))),
            ("Xonadonlarga tarqatilgan parrandalar", text(sum(m.quantity for m in movements))),
            ("Tasdiqlangan inventarizatsiyalar", text(len(counts))),
        ],
        sections=[
            Section(
                "Kirim",
                ["Sana", "Kod", "Hujjat", "Mahsulot", "Miqdor", "Kiritgan"],
                [[fmt_date(r.created_at), r.op_id, r.document_no, r.product.name, qty(r), name_of(r.created_by)]
                 for r in receipts],
            ),
            Section(
                "Chiqim (tarqatish)",
                ["Sana", "Kod", "Hujjat", "Mahsulot", "Miqdor", "Xonadon"],
                [[fmt_date(r.created_at), r.op_id, r.document_no, r.product.name, qty(r), household_label(r.household)]
                 for r in issues],
            ),
            Section(
                "Xonadonlarga tarqatilgan parrandalar",
                ["Sana", "Kod", "Partiya", "Xonadon", "Soni"],
                [[fmt_date(m.date), m.op_id, m.batch.code, household_label(m.household), text(m.quantity)]
                 for m in movements],
            ),
            Section(
                "Tasdiqlangan inventarizatsiyalar",
                ["Sana", "Kod", "Mahsulotlar", "Farqli qatorlar", "Tasdiqladi"],
                [
                    [fmt_date(c.approved_at), c.code, text(len(c.lines.all())),
                     text(sum(1 for line in c.lines.all() if line.difference)), name_of(c.approved_by)]
                    for c in counts
                ],
            ),
        ],
    )


# --- LOGISTICS ---------------------------------------------------------------------------------


def _delivered_tasks(w: Window) -> list[LogisticsTask]:
    tasks = w.households(LogisticsTask.objects.filter(status__in=DELIVERED_STATUSES).filter(w.moments("delivered_at")))
    return list(
        tasks.select_related("household", "driver", "vehicle", "responsible")
        .prefetch_related("items__product")
        .order_by("delivered_at", "id")
    )


def _items_text(task: LogisticsTask) -> str:
    return "; ".join(f"{item.product.name} {text(item.quantity)} {item.product.unit}" for item in task.items.all())


def logistics_content(w: Window) -> RoleContent:
    tasks = _delivered_tasks(w)
    if w.user is not None:
        tasks = [
            t for t in tasks
            if t.responsible_id == w.user.pk or (t.driver is not None and t.driver.user_id == w.user.pk)
        ]
    total_kg = sum(
        (item.quantity for t in tasks for item in t.items.all() if item.product.unit == "kg"), Decimal(0)
    )
    return RoleContent(
        summary=[
            ("Yetkazilgan topshiriqlar", text(len(tasks))),
            ("Xonadon qabul qilganlari", text(sum(1 for t in tasks if t.status == LogisticsStatus.CONFIRMED))),
            ("Yetkazilgan yuk, kg", text(total_kg)),
        ],
        sections=[
            Section(
                "Yetkazilgan topshiriqlar",
                ["Sana", "Kod", "Xonadon", "Mahsulotlar", "Miqdor", "Haydovchi / transport"],
                [
                    [fmt_date(t.delivered_at), t.code or DASH, household_label(t.household), _items_text(t),
                     text(sum((item.quantity for item in t.items.all()), Decimal(0))),
                     f"{t.driver.full_name if t.driver else DASH} / {t.vehicle.plate_number if t.vehicle else DASH}"]
                    for t in tasks
                ],
            )
        ],
    )


# --- VET ---------------------------------------------------------------------------------------


def vet_content(w: Window) -> RoleContent:
    visits = w.households(VetVisit.objects.filter(status=VetStatus.DONE).filter(w.moments("done_at")))
    vaccinations = w.households(Vaccination.objects.filter(status=VetStatus.DONE).filter(w.days("done_date")))
    diseases = w.households(DiseaseRecord.objects.filter(w.days("diagnosed_date")))
    requests = w.households(VetRequest.objects.filter(status=RequestStatus.CLOSED).filter(w.moments("closed_at")))
    if w.user is not None:
        visits = visits.filter(vet=w.user)
        vaccinations = vaccinations.filter(vet=w.user)
        diseases = diseases.filter(Q(vet=w.user) | Q(vet__isnull=True, created_by=w.user))
        requests = requests.filter(Q(closed_by=w.user) | Q(vet=w.user))
    visits = list(visits.select_related("household").order_by("done_at", "id"))
    vaccinations = list(vaccinations.select_related("household").order_by("done_date", "id"))
    diseases = list(diseases.select_related("household").order_by("diagnosed_date", "id"))
    requests = list(requests.select_related("household").order_by("closed_at", "id"))
    return RoleContent(
        summary=[
            ("O‘tkazilgan tekshiruvlar", text(len(visits))),
            ("Tekshirilgan parrandalar", text(sum(v.birds_checked or 0 for v in visits))),
            ("O‘tkazilgan emlashlar", text(len(vaccinations))),
            ("Emlangan parrandalar", text(sum(v.birds_vaccinated or 0 for v in vaccinations))),
            ("Kasallik qaydlari", text(len(diseases))),
            ("Yopilgan murojaatlar", text(len(requests))),
        ],
        sections=[
            Section(
                "Tekshiruvlar",
                ["Sana", "Kod", "Xonadon", "Tekshirilgan", "Holat", "Xulosa"],
                [[fmt_date(v.done_at), v.code, household_label(v.household), text(v.birds_checked),
                  text(v.get_health_status_display()), text(v.conclusion)] for v in visits],
            ),
            Section(
                "Emlashlar",
                ["Sana", "Kod", "Xonadon", "Vaksina", "Emlangan"],
                [[fmt_date(v.done_date), v.code, household_label(v.household), text(v.vaccine_name),
                  text(v.birds_vaccinated)] for v in vaccinations],
            ),
            Section(
                "Kasalliklar",
                ["Sana", "Kod", "Xonadon", "Kasallik", "Holatlar", "Holati"],
                [[fmt_date(d.diagnosed_date), d.code, household_label(d.household), text(d.disease_name),
                  text(d.cases), d.get_status_display()] for d in diseases],
            ),
            Section(
                "Yopilgan murojaatlar",
                ["Sana", "Kod", "Xonadon", "Murojaat", "Javob"],
                [[fmt_date(r.closed_at), r.code, household_label(r.household), text(r.text), text(r.response)]
                 for r in requests],
            ),
        ],
    )


# --- OWNER -------------------------------------------------------------------------------------


def owner_content(w: Window) -> RoleContent:
    reports = list(
        w.households(DailyReport.objects.filter(w.days("date"))).select_related("household").order_by("date", "id")
    )
    weights = [r.avg_weight_g for r in reports if r.avg_weight_g is not None]
    deliveries = _delivered_tasks(w)
    requests = list(
        w.households(VetRequest.objects.filter(w.moments("created_at"))).select_related("household")
        .order_by("created_at", "id")
    )
    meat = sum((r.meat_kg or Decimal(0) for r in reports), Decimal(0))
    feed = sum((r.feed_kg for r in reports), Decimal(0))
    avg_weight = (sum(weights, Decimal(0)) / len(weights)).quantize(Decimal("0.1")) if weights else None
    return RoleContent(
        summary=[
            ("Kunlik hisobotlar", text(len(reports))),
            ("Go‘sht, kg", text(meat)),
            ("Yem, kg", text(feed)),
            ("Nobud bo‘lgan", text(sum(r.deaths for r in reports))),
            ("O‘rtacha vazn, g", text(avg_weight)),
            ("Qabul qilingan yetkazmalar", text(len(deliveries))),
            ("Veterinarga murojaatlar", text(len(requests))),
        ],
        sections=[
            Section(
                "Kunlik hisobotlar",
                ["Sana", "Xonadon", "Parranda", "Yem, kg", "Go‘sht, kg", "Nobud", "O‘rtacha vazn, g"],
                [[fmt_date(r.date), household_label(r.household), text(r.bird_count), text(r.feed_kg),
                  text(r.meat_kg), text(r.deaths), text(r.avg_weight_g)] for r in reports],
            ),
            Section(
                "Qabul qilingan yetkazmalar",
                ["Sana", "Kod", "Xonadon", "Mahsulotlar", "Holati"],
                [[fmt_date(t.delivered_at), t.code or DASH, household_label(t.household), _items_text(t),
                  t.get_status_display()] for t in deliveries],
            ),
            Section(
                "Veterinarga murojaatlar",
                ["Sana", "Kod", "Xonadon", "Murojaat", "Holati", "Javob"],
                [[fmt_date(r.created_at), r.code, household_label(r.household), text(r.text),
                  r.get_status_display(), text(r.response)] for r in requests],
            ),
        ],
    )


BUILDERS = {
    Role.ADMIN: admin_content,
    Role.WAREHOUSE: warehouse_content,
    Role.LOGISTICS: logistics_content,
    Role.VET: vet_content,
    Role.OWNER: owner_content,
}

#: Display order of the roles in the general act.
ROLE_ORDER = (Role.ADMIN, Role.WAREHOUSE, Role.LOGISTICS, Role.VET, Role.OWNER)


# --- Builder tasks (V4 §3) ---------------------------------------------------------------------


def _role_refs(role: str) -> list[str]:
    """``"VET"`` plus ``"profile:<id>"`` of every role profile whose base role is ``role``."""
    profiles = Blueprint.objects.filter(kind=BlueprintKind.ROLE).values_list("pk", "data")
    return [role] + [f"profile:{pk}" for pk, data in profiles if isinstance(data, dict) and data.get("base_role") == role]


def approved_tasks(w: Window, role: str | None) -> list[TaskInstance]:
    """APPROVED, non-test builder tasks decided in the period whose executor matches ``role`` / the user.

    ``role=None`` (general act) takes every role.
    """
    qs = w.households(
        TaskInstance.objects.filter(status=TaskStatus.APPROVED, is_test=False).filter(w.moments("decided_at"))
    )
    if role is not None:
        match = Q(executor__in=_role_refs(role))
        if role == Role.ADMIN:
            match |= Q(decided_by__role=Role.ADMIN) | Q(decided_by__is_superuser=True)
        qs = qs.filter(match)
    if w.user is not None:
        user_match = Q(assigned_to=w.user) | Q(submitted_by=w.user)
        if role == Role.ADMIN:
            user_match |= Q(decided_by=w.user)
        qs = qs.filter(user_match)
    return list(
        qs.select_related("household", "assigned_to", "submitted_by", "decided_by").order_by("decided_at", "id")
    )


def latest_submissions(tasks: list[TaskInstance]) -> dict[int, Any]:
    """``{task_id: latest FormSubmission}`` in one query."""
    from builder.models import FormSubmission

    latest: dict[int, Any] = {}
    rows = FormSubmission.objects.filter(task_id__in=[t.pk for t in tasks]).order_by("task_id", "-created_at", "-id")
    for row in rows:
        latest.setdefault(row.task_id, row)
    return latest


def submission_values(submission, file_names: dict[int, str]) -> tuple[list[dict], list[dict], dict[str, str]]:
    """``(values, photos, by_key)`` of a submission: display strings, photo file ids, ``{key: value}``."""
    values: list[dict] = []
    photos: list[dict] = []
    by_key: dict[str, str] = {}
    if submission is None:
        return values, photos, by_key
    data = submission.data if isinstance(submission.data, dict) else {}
    for spec in submission.fields if isinstance(submission.fields, list) else []:
        if not isinstance(spec, dict) or not spec.get("key"):
            continue
        key, label, kind = spec["key"], str(spec.get("label") or spec["key"]), spec.get("type")
        raw = data.get(key)
        display = display_value(kind, raw, file_names)
        values.append({"label": label, "value": display})
        by_key[key] = display
        if kind == "photo" and isinstance(raw, int) and not isinstance(raw, bool):
            photos.append({"file_id": raw, "label": label})
    return values, photos, by_key


def display_value(kind: str | None, raw: Any, file_names: dict[int, str]) -> str:
    if raw is None or raw == "" or raw == []:
        return DASH
    if kind in ("file", "photo") and isinstance(raw, int):
        return file_names.get(raw, f"#{raw}")
    if kind == "date" and isinstance(raw, str):
        parts = raw.split("-")
        return ".".join(reversed(parts)) if len(parts) == 3 else raw
    if kind == "geo" and isinstance(raw, dict):
        return f"{raw.get('lat')}, {raw.get('lng')}"
    if isinstance(raw, list):
        return ", ".join(str(item) for item in raw)
    return text(raw)


def file_names_of(submissions) -> dict[int, str]:
    from core.models import Attachment

    ids: set[int] = set()
    for submission in submissions:
        for spec in submission.fields if isinstance(submission.fields, list) else []:
            if isinstance(spec, dict) and spec.get("type") in ("file", "photo"):
                value = (submission.data or {}).get(spec.get("key"))
                if isinstance(value, int) and not isinstance(value, bool):
                    ids.add(value)
    if not ids:
        return {}
    return dict(Attachment.objects.filter(pk__in=ids).values_list("pk", "original_name"))


def tasks_block(tasks: list[TaskInstance]) -> tuple[list[dict], list[dict]]:
    """Snapshot ``tasks`` and ``photos`` of approved builder tasks (latest submission values)."""
    submissions = latest_submissions(tasks)
    names = file_names_of(submissions.values())
    items: list[dict] = []
    photos: list[dict] = []
    for task in tasks:
        values, task_photos, _ = submission_values(submissions.get(task.pk), names)
        items.append(
            {
                "code": task.code,
                "title": task.title,
                "household": household_label(task.household),
                "approved_at": fmt_moment(task.decided_at),
                "values": values,
            }
        )
        photos.extend({"file_id": p["file_id"], "label": f"{task.code}: {p['label']}"} for p in task_photos)
    return items, photos


def counted(content: RoleContent, tasks: int) -> int:
    """Number of work items of a role (table rows + approved tasks), for the general act totals."""
    return sum(len(section.rows) for section in content.sections) + tasks


__all__ = [
    "BUILDERS",
    "ROLE_ORDER",
    "RoleContent",
    "Section",
    "Window",
    "approved_tasks",
    "counted",
    "fmt_moment",
    "household_label",
    "submission_values",
    "tasks_block",
    "text",
]

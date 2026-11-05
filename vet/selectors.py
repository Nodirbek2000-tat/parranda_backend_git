"""Read side of the vet app (API_CONTRACT_V3 §8): scoped querysets, calendar, summaries, dashboard counters.

Scoping: ADMIN and VET see everything; an OWNER only rows of his own household (other ids → 404 because the
row is simply not in the queryset); WAREHOUSE only ``GET /api/vet/vaccinations?batch=`` (checked in the view).
Every function runs a fixed number of queries (§17.3 budgets: lists ≤ 6, summary ≤ 10, calendar ≤ 3).
"""

from __future__ import annotations

from collections import defaultdict
from datetime import date, timedelta
from typing import Any

from django.db.models import Case, Count, IntegerField, OuterRef, Q, QuerySet, Subquery, Sum, Value, When
from django.db.models.functions import Coalesce
from django.utils import timezone

from accounts.models import User
from common.periods import day_start
from common.roles import Role, role_of
from farm.models import Household, MovementType, PoultryMovement

from .models import (
    DiseaseRecord,
    DiseaseStatus,
    RequestStatus,
    Treatment,
    Vaccination,
    VetRequest,
    VetStatus,
    VetVisit,
)

LIST_LIMIT = 10
OWNER_UPCOMING_LIMIT = 5
OWNER_REQUESTS_LIMIT = 10
CALENDAR_MAX_DAYS = 62
DUE_DAYS = 7  # "7 kun ichida" = today … today + 6
DONE_DAYS = 30


def today() -> date:
    return timezone.localdate()


# --- Querysets -----------------------------------------------------------------------------------


def scope(qs: QuerySet, user: User) -> QuerySet:
    """OWNER → own household only (404 for other ids); other roles unchanged (permissions decide)."""
    if role_of(user) == Role.OWNER:
        return qs.filter(household__owner=user)
    return qs


def visits() -> QuerySet[VetVisit]:
    return VetVisit.objects.select_related("household", "batch", "vet", "created_by", "request")


def vaccinations() -> QuerySet[Vaccination]:
    return Vaccination.objects.select_related(
        "household", "batch", "vet", "product", "created_by", "stock_transaction"
    )


def _deaths_subquery(*, with_batch: bool) -> Subquery:
    condition = {
        "household_id": OuterRef("household_id"),
        "type": MovementType.DEATH,
        "date__gte": OuterRef("diagnosed_date"),
    }
    if with_batch:
        condition["batch_id"] = OuterRef("batch_id")
    rows = (
        PoultryMovement.objects.filter(**condition)
        .order_by()
        .values("household_id")
        .annotate(total=Sum("quantity"))
        .values("total")
    )
    return Subquery(rows, output_field=IntegerField())


def diseases() -> QuerySet[DiseaseRecord]:
    """``deaths_since_diagnosis`` = Σ DEATH movements of the household (and batch, if set) ≥ diagnosed_date."""
    return DiseaseRecord.objects.select_related("household", "batch", "vet", "visit", "created_by").annotate(
        deaths_since_diagnosis=Case(
            When(batch__isnull=True, then=Coalesce(_deaths_subquery(with_batch=False), Value(0))),
            default=Coalesce(_deaths_subquery(with_batch=True), Value(0)),
            output_field=IntegerField(),
        )
    )


def treatments() -> QuerySet[Treatment]:
    return Treatment.objects.select_related(
        "household", "batch", "disease", "vet", "product", "created_by", "stock_transaction"
    )


def requests() -> QuerySet[VetRequest]:
    return VetRequest.objects.select_related("household", "vet", "created_by", "closed_by")


def vet_filter(qs: QuerySet, user: User, raw: str) -> QuerySet:
    """``vet=me`` → the caller; ``vet=<id>``; ``vet=none`` → unassigned."""
    raw = (raw or "").strip().lower()
    if not raw:
        return qs
    if raw == "me":
        return qs.filter(vet=user)
    if raw in ("none", "null", "0"):
        return qs.filter(vet__isnull=True)
    try:
        return qs.filter(vet_id=int(raw))
    except ValueError:
        from rest_framework.exceptions import ValidationError

        raise ValidationError({"vet": ["“me” yoki veterinar id raqamini kiriting."]}) from None


def mine_or_unassigned(qs: QuerySet, user: User) -> QuerySet:
    return qs.filter(Q(vet=user) | Q(vet__isnull=True))


# --- Calendar (§8.7) -----------------------------------------------------------------------------


def calendar(date_from: date, date_to: date, *, user: User, vet: str = "") -> list[dict[str, Any]]:
    """One item per day (zero-filled, oldest first); cancelled items excluded. 2 queries."""
    current = today()
    visit_rows = vet_filter(
        VetVisit.objects.select_related("household", "vet").filter(
            planned_date__gte=date_from, planned_date__lte=date_to
        ).exclude(status=VetStatus.CANCELLED),
        user,
        vet,
    ).order_by("planned_date", "id")
    vaccination_rows = vet_filter(
        Vaccination.objects.select_related("household").filter(
            planned_date__gte=date_from, planned_date__lte=date_to
        ).exclude(status=VetStatus.CANCELLED),
        user,
        vet,
    ).order_by("planned_date", "id")
    by_day: dict[date, dict[str, list]] = defaultdict(lambda: {"visits": [], "vaccinations": []})
    for visit in visit_rows:
        by_day[visit.planned_date]["visits"].append(
            {
                "id": visit.pk,
                "code": visit.code,
                "household": household_short(visit.household),
                "status": visit.status,
                "vet": user_ref(visit.vet),
                "is_overdue": visit.status == VetStatus.PLANNED and visit.planned_date < current,
            }
        )
    for row in vaccination_rows:
        by_day[row.planned_date]["vaccinations"].append(
            {
                "id": row.pk,
                "code": row.code,
                "household": household_short(row.household),
                "vaccine_name": row.vaccine_name,
                "status": row.status,
                "is_overdue": row.status == VetStatus.PLANNED and row.planned_date < current,
            }
        )
    days = []
    day = date_from
    while day <= date_to:
        items = by_day.get(day) or {"visits": [], "vaccinations": []}
        days.append({"date": day.isoformat(), **items})
        day += timedelta(days=1)
    return days


# --- Short objects -------------------------------------------------------------------------------


def household_short(household: Household) -> dict[str, Any]:
    return {"id": household.pk, "code": household.code, "name": household.name}


def user_ref(user: User | None) -> dict[str, Any] | None:
    return {"id": user.pk, "full_name": user.full_name} if user is not None else None


def visit_short(visit: VetVisit, user: User | None = None) -> dict[str, Any]:
    current = today()
    return {
        "id": visit.pk,
        "code": visit.code,
        "household": household_short(visit.household),
        "batch": {"id": visit.batch.pk, "code": visit.batch.code} if visit.batch_id else None,
        "vet": user_ref(visit.vet),
        "planned_date": visit.planned_date.isoformat(),
        "purpose": visit.purpose,
        "status": visit.status,
        "status_display": visit.get_status_display(),
        "is_overdue": visit.status == VetStatus.PLANNED and visit.planned_date < current,
        "mine": bool(user is not None and visit.vet_id == user.pk),
    }


def vaccination_short(row: Vaccination, user: User | None = None) -> dict[str, Any]:
    current = today()
    return {
        "id": row.pk,
        "code": row.code,
        "household": household_short(row.household),
        "vet": user_ref(row.vet),
        "vaccine_name": row.vaccine_name,
        "dose": row.dose,
        "planned_date": row.planned_date.isoformat(),
        "birds_planned": row.birds_planned,
        "status": row.status,
        "status_display": row.get_status_display(),
        "is_overdue": row.status == VetStatus.PLANNED and row.planned_date < current,
        "mine": bool(user is not None and row.vet_id == user.pk),
    }


def request_short(row: VetRequest) -> dict[str, Any]:
    text = row.text.strip()
    return {
        "id": row.pk,
        "code": row.code,
        "household": household_short(row.household),
        "text": text if len(text) <= 160 else f"{text[:160].rstrip()}…",
        "status": row.status,
        "status_display": row.get_status_display(),
        "created_at": timezone.localtime(row.created_at).isoformat(),
    }


# --- Counters ------------------------------------------------------------------------------------


def _visit_counts(current: date) -> dict[str, int]:
    week_end = current + timedelta(days=DUE_DAYS - 1)
    done_since = current - timedelta(days=DONE_DAYS - 1)
    row = VetVisit.objects.aggregate(
        today=Count("id", filter=Q(planned_date=current) & ~Q(status=VetStatus.CANCELLED)),
        planned_7d=Count("id", filter=Q(status=VetStatus.PLANNED, planned_date__gte=current, planned_date__lte=week_end)),
        overdue=Count("id", filter=Q(status=VetStatus.PLANNED, planned_date__lt=current)),
        done_30d=Count("id", filter=Q(status=VetStatus.DONE, done_at__gte=day_start(done_since))),
    )
    return {key: int(value or 0) for key, value in row.items()}


def _vaccination_counts(current: date) -> dict[str, int]:
    week_end = current + timedelta(days=DUE_DAYS - 1)
    row = Vaccination.objects.aggregate(
        due_7d=Count("id", filter=Q(status=VetStatus.PLANNED, planned_date__gte=current, planned_date__lte=week_end)),
        overdue=Count("id", filter=Q(status=VetStatus.PLANNED, planned_date__lt=current)),
        done_30d=Count(
            "id", filter=Q(status=VetStatus.DONE, done_date__gte=current - timedelta(days=DONE_DAYS - 1))
        ),
    )
    return {key: int(value or 0) for key, value in row.items()}


def _disease_counts(current: date) -> dict[str, int]:
    row = DiseaseRecord.objects.aggregate(
        open=Count("id", filter=Q(status=DiseaseStatus.OPEN)),
        treatment=Count("id", filter=Q(status=DiseaseStatus.TREATMENT)),
        resolved_30d=Count(
            "id",
            filter=Q(status=DiseaseStatus.RESOLVED, resolved_date__gte=current - timedelta(days=DONE_DAYS - 1)),
        ),
    )
    return {key: int(value or 0) for key, value in row.items()}


def _request_counts() -> dict[str, int]:
    row = VetRequest.objects.aggregate(
        new=Count("id", filter=Q(status=RequestStatus.NEW)),
        in_progress=Count("id", filter=Q(status=RequestStatus.IN_PROGRESS)),
    )
    return {key: int(value or 0) for key, value in row.items()}


def summary(user: User, current: date | None = None) -> dict[str, Any]:
    """``GET /api/vet/summary`` (A, V): company-wide numbers, lists ≤ 10 with ``mine``. 9 queries."""
    current = current or today()
    week_end = current + timedelta(days=DUE_DAYS - 1)
    planned_visits = VetVisit.objects.select_related("household", "batch", "vet").filter(status=VetStatus.PLANNED)
    planned_vaccinations = Vaccination.objects.select_related("household", "vet").filter(status=VetStatus.PLANNED)
    return {
        "date": current.isoformat(),
        "visits": _visit_counts(current),
        "vaccinations": _vaccination_counts(current),
        "diseases": _disease_counts(current),
        "requests": _request_counts(),
        "today_visits": [
            visit_short(v, user)
            for v in VetVisit.objects.select_related("household", "batch", "vet")
            .filter(planned_date=current)
            .exclude(status=VetStatus.CANCELLED)
            .order_by("status", "id")[:LIST_LIMIT]
        ],
        "overdue_visits": [
            visit_short(v, user)
            for v in planned_visits.filter(planned_date__lt=current).order_by("planned_date", "id")[:LIST_LIMIT]
        ],
        "due_vaccinations": [
            vaccination_short(v, user)
            for v in planned_vaccinations.filter(planned_date__gte=current, planned_date__lte=week_end).order_by(
                "planned_date", "id"
            )[:LIST_LIMIT]
        ],
        "overdue_vaccinations": [
            vaccination_short(v, user)
            for v in planned_vaccinations.filter(planned_date__lt=current).order_by("planned_date", "id")[:LIST_LIMIT]
        ],
        "new_requests": [
            request_short(r)
            for r in VetRequest.objects.select_related("household")
            .filter(status=RequestStatus.NEW)
            .order_by("created_at", "id")[:LIST_LIMIT]
        ],
    }


def dashboard_counts(current: date | None = None) -> dict[str, int]:
    """``vet`` block of ``GET /api/dashboard/admin`` (§10.1). 3 queries."""
    current = current or today()
    week_end = current + timedelta(days=DUE_DAYS - 1)
    visit_row = VetVisit.objects.aggregate(
        visits_today=Count("id", filter=Q(planned_date=current) & ~Q(status=VetStatus.CANCELLED)),
        visits_planned_7d=Count(
            "id", filter=Q(status=VetStatus.PLANNED, planned_date__gte=current, planned_date__lte=week_end)
        ),
        visits_overdue=Count("id", filter=Q(status=VetStatus.PLANNED, planned_date__lt=current)),
    )
    vaccination_row = Vaccination.objects.aggregate(
        vaccinations_due_7d=Count(
            "id", filter=Q(status=VetStatus.PLANNED, planned_date__gte=current, planned_date__lte=week_end)
        ),
        vaccinations_overdue=Count("id", filter=Q(status=VetStatus.PLANNED, planned_date__lt=current)),
    )
    other = {
        "open_diseases": DiseaseRecord.objects.exclude(status=DiseaseStatus.RESOLVED).count(),
        "new_requests": VetRequest.objects.filter(status=RequestStatus.NEW).count(),
    }
    return {
        **{key: int(value or 0) for key, value in visit_row.items()},
        **{key: int(value or 0) for key, value in vaccination_row.items()},
        **other,
    }


def problem_counts(household_ids: list[int] | None = None, current: date | None = None) -> dict[int, dict[str, int]]:
    """Per household: ``overdue_vaccinations`` and ``open_diseases`` (problem households, §10.1). 2 queries."""
    current = current or today()
    result: dict[int, dict[str, int]] = defaultdict(lambda: {"overdue_vaccinations": 0, "open_diseases": 0})
    vaccinations_qs = Vaccination.objects.filter(status=VetStatus.PLANNED, planned_date__lt=current)
    diseases_qs = DiseaseRecord.objects.exclude(status=DiseaseStatus.RESOLVED)
    if household_ids is not None:
        vaccinations_qs = vaccinations_qs.filter(household_id__in=household_ids)
        diseases_qs = diseases_qs.filter(household_id__in=household_ids)
    for hid, count in vaccinations_qs.order_by().values_list("household_id").annotate(n=Count("id")):
        result[hid]["overdue_vaccinations"] = count
    for hid, count in diseases_qs.order_by().values_list("household_id").annotate(n=Count("id")):
        result[hid]["open_diseases"] = count
    return dict(result)


def owner_dashboard(household: Household, current: date | None = None) -> dict[str, Any]:
    """``vet`` block of ``GET /api/dashboard/owner`` (§10.2). 3 queries."""
    current = current or today()
    next_visit = (
        VetVisit.objects.select_related("household", "batch", "vet")
        .filter(household=household, status=VetStatus.PLANNED)
        .order_by("planned_date", "id")
        .first()
    )
    next_vaccination = (
        Vaccination.objects.select_related("household", "vet")
        .filter(household=household, status=VetStatus.PLANNED)
        .order_by("planned_date", "id")
        .first()
    )
    return {
        "next_visit": visit_short(next_visit) if next_visit else None,
        "next_vaccination": vaccination_short(next_vaccination) if next_vaccination else None,
        "open_requests": VetRequest.objects.filter(
            household=household, status__in=[RequestStatus.NEW, RequestStatus.IN_PROGRESS]
        ).count(),
    }


def tables_ready() -> bool:
    """The vet tables exist (``False`` on the live database before the Fix-phase ``migrate``)."""
    from warehouse.readiness import models_ready

    return models_ready(VetVisit, Vaccination, DiseaseRecord, Treatment, VetRequest)


DASHBOARD_ZEROS = {
    "visits_today": 0,
    "visits_planned_7d": 0,
    "visits_overdue": 0,
    "vaccinations_due_7d": 0,
    "vaccinations_overdue": 0,
    "open_diseases": 0,
    "new_requests": 0,
}


def admin_dashboard_block() -> dict[str, Any]:
    """``{"vet": {...}}`` for ``GET /api/dashboard/admin`` (§10.1); zeros until the vet tables are migrated."""
    return {"vet": dashboard_counts() if tables_ready() else dict(DASHBOARD_ZEROS)}


def owner_dashboard_block(household: Household) -> dict[str, Any]:
    """``vet`` of ``GET /api/dashboard/owner`` (§10.2); empty until the vet tables are migrated."""
    if not tables_ready():
        return {"next_visit": None, "next_vaccination": None, "open_requests": 0}
    return owner_dashboard(household)


def bot_today(current: date | None = None) -> dict[str, int]:
    """``today`` of the VET bot summary (§10.3)."""
    counts = dashboard_counts(current) if tables_ready() else DASHBOARD_ZEROS
    return {
        "visits_today": counts["visits_today"],
        "visits_overdue": counts["visits_overdue"],
        "vaccinations_due_7d": counts["vaccinations_due_7d"],
        "vaccinations_overdue": counts["vaccinations_overdue"],
        "open_diseases": counts["open_diseases"],
        "new_requests": counts["new_requests"],
    }


def bot_tasks(user: User, current: date | None = None) -> dict[str, Any]:
    """``GET /api/bot/users/{tid}/tasks`` for a VET (§14.2): own + unassigned work, each list ≤ 10."""
    current = current or today()
    tomorrow = current + timedelta(days=1)
    planned_visits = mine_or_unassigned(
        VetVisit.objects.select_related("household", "batch", "vet").filter(status=VetStatus.PLANNED), user
    )
    planned_vaccinations = mine_or_unassigned(
        Vaccination.objects.select_related("household", "vet").filter(status=VetStatus.PLANNED), user
    )
    return {
        "role": Role.VET.value,
        "date": current.isoformat(),
        "visits_today": [
            visit_short(v, user) for v in planned_visits.filter(planned_date=current).order_by("id")[:LIST_LIMIT]
        ],
        "visits_overdue": [
            visit_short(v, user)
            for v in planned_visits.filter(planned_date__lt=current).order_by("planned_date", "id")[:LIST_LIMIT]
        ],
        "vaccinations_due": [
            vaccination_short(v, user)
            for v in planned_vaccinations.filter(planned_date__gte=current, planned_date__lte=tomorrow).order_by(
                "planned_date", "id"
            )[:LIST_LIMIT]
        ],
        "vaccinations_overdue": [
            vaccination_short(v, user)
            for v in planned_vaccinations.filter(planned_date__lt=current).order_by("planned_date", "id")[:LIST_LIMIT]
        ],
        "new_requests": VetRequest.objects.filter(status=RequestStatus.NEW).count(),
    }


def admin_vet_overdue(current: date | None = None) -> int:
    """ADMIN ``/tasks``: overdue visits + overdue vaccinations (§14.2 ``vet_overdue``)."""
    current = current or today()
    return (
        VetVisit.objects.filter(status=VetStatus.PLANNED, planned_date__lt=current).count()
        + Vaccination.objects.filter(status=VetStatus.PLANNED, planned_date__lt=current).count()
    )

"""Daily vet reminder jobs (API_CONTRACT_V3 §16.1), registered in :class:`vet.apps.VetConfig`.

Each job runs in its own transaction (``notifications.reminders.run``) and returns the number of queued rows.
Idempotent per day and stage through the event keys:

- ``vaccination_due``: PLANNED with ``today ≤ planned_date ≤ today + vaccination_reminder_days`` → key
  ``{id}.{YYYYMMDD}.due`` (once per planned date) and ``{id}.{YYYYMMDD}.today`` on the planned day;
- ``vaccination_overdue``: PLANNED, ``(today − planned_date).days ∈ {1, 7, 14, 30}`` → key ``{id}.{YYYYMMDD}.od{days}``
  (the planned date is in every key, so a rescheduled vaccination is reminded again for its new date);
- ``vet_visit_today``: PLANNED visits of today → key ``{id}.{YYYYMMDD}``.
"""

from __future__ import annotations

from datetime import date, timedelta

from core.settings_service import get_settings

from . import events
from .models import Vaccination, VetStatus, VetVisit

OVERDUE_STAGES = (1, 7, 14, 30)


def vaccination_due_job(today: date) -> int:
    days = get_settings().vaccination_reminder_days
    rows = (
        Vaccination.objects.select_related("household__owner", "batch", "vet")
        .filter(status=VetStatus.PLANNED, planned_date__gte=today, planned_date__lte=today + timedelta(days=days))
        .order_by("planned_date", "id")
    )
    return sum(len(events.vaccination_due(row, today)) for row in rows)


def vaccination_overdue_job(today: date) -> int:
    stage_dates = {today - timedelta(days=stage): stage for stage in OVERDUE_STAGES}
    rows = (
        Vaccination.objects.select_related("household__owner", "batch", "vet")
        .filter(status=VetStatus.PLANNED, planned_date__in=list(stage_dates))
        .order_by("planned_date", "id")
    )
    return sum(len(events.vaccination_overdue(row, stage_dates[row.planned_date])) for row in rows)


def vet_visit_today_job(today: date) -> int:
    rows = (
        VetVisit.objects.select_related("household__owner", "batch", "vet")
        .filter(status=VetStatus.PLANNED, planned_date=today)
        .order_by("id")
    )
    return sum(len(events.visit_today(row, today)) for row in rows)


def register() -> None:
    from notifications import reminders

    reminders.register_job("vaccination_due", vaccination_due_job, order=10)
    reminders.register_job("vaccination_overdue", vaccination_overdue_job, order=11)
    reminders.register_job("vet_visit_today", vet_visit_today_job, order=12)

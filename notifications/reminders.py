"""Daily reminders (API_CONTRACT_V3 §16.1): run guard + a job registry the modules plug into.

The guard lives here; the jobs live in their modules and register themselves in ``AppConfig.ready()``::

    from notifications import reminders

    def vaccination_reminders(today: date) -> int:
        ...  # call notify_on_commit(...) per row with keys like f"{vaccination.pk}.due"; return how many queued
        return queued

    reminders.register_job("vaccination_due", vaccination_reminders, order=10)

``run_if_due(now)`` (bot tick ``POST /api/bot/reminders/run`` every 15 min and ``manage.py send_reminders``)
runs the jobs once per day after ``SystemSettings.reminder_hour``. The settings row is locked with
``select_for_update()`` so two concurrent ticks never both run; ``event_key`` uniqueness keeps a forced
re-run idempotent.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime

from django.db import transaction
from django.utils import timezone

logger = logging.getLogger("nnp.reminders")

Job = Callable[[date], int]


@dataclass(frozen=True)
class RegisteredJob:
    name: str
    fn: Job
    order: int


_JOBS: dict[str, RegisteredJob] = {}


def register_job(name: str, fn: Job, *, order: int = 100) -> None:
    """Add (or replace) a daily job. ``fn(today)`` queues notifications and returns the number queued."""
    _JOBS[name] = RegisteredJob(name, fn, order)


def jobs() -> list[RegisteredJob]:
    return sorted(_JOBS.values(), key=lambda job: (job.order, job.name))


def run(today: date) -> int:
    """Run every job for ``today`` (no guard). A failing job is logged and does not stop the others."""
    queued = 0
    for job in jobs():
        try:
            with transaction.atomic():
                queued += int(job.fn(today) or 0)
        except Exception:  # noqa: BLE001 - one broken job must not block the other reminders
            logger.exception("Reminder job %s failed", job.name)
    return queued


def run_if_due(now: datetime | None = None, *, force: bool = False, move_guard: bool = True) -> dict:
    """``{"date": "YYYY-MM-DD", "ran": bool, "queued": int}``.

    Runs only when ``now.hour >= reminder_hour`` and ``last_reminders_date < today`` (or ``force``).
    The guard row is locked, so concurrent ticks serialize and only the first one runs.
    The guard only ever moves forward; ``move_guard=False`` (a manual run for another date) leaves it
    untouched, so the real daily run is never skipped or shifted.
    """
    from core.settings_service import lock_settings

    now = timezone.localtime(now or timezone.now())
    today = now.date()
    with transaction.atomic():
        row = lock_settings()
        due = force or (
            now.hour >= row.reminder_hour and (row.last_reminders_date is None or row.last_reminders_date < today)
        )
        if not due:
            return {"date": today.isoformat(), "ran": False, "queued": 0}
        if move_guard and (row.last_reminders_date is None or row.last_reminders_date < today):
            row.last_reminders_date = today
            row.save(update_fields=["last_reminders_date"])
    # Jobs run after the guard commits: a crash in a job cannot unlock a second run the same day,
    # and the per-row event keys make a manual --force re-run safe.
    queued = run(today)
    return {"date": today.isoformat(), "ran": True, "queued": queued}

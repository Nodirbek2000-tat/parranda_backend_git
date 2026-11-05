"""Local-day helpers (Asia/Tashkent): date ↔ half-open datetime ranges."""

from __future__ import annotations

from datetime import date, datetime, time, timedelta

from django.utils import timezone


def day_start(day: date) -> datetime:
    """Aware datetime of local 00:00 of ``day`` in the current time zone (Asia/Tashkent)."""
    return timezone.make_aware(datetime.combine(day, time.min), timezone.get_current_timezone())


def day_range(date_from: date | None, date_to: date | None) -> tuple[datetime | None, datetime | None]:
    """Half-open range ``[local 00:00 of date_from, local 00:00 of date_to + 1 day)``."""
    start = day_start(date_from) if date_from is not None else None
    end = day_start(date_to + timedelta(days=1)) if date_to is not None else None
    return start, end

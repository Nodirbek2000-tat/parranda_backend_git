"""Company settings singleton (API_CONTRACT_V3 §3).

    from core.settings_service import get_settings
    if get_settings().delivery_proof_required: ...
"""

from __future__ import annotations

from django.db import transaction

from accounts.models import User
from audit import services as audit
from audit.models import AuditAction, AuditModule

from .models import SystemSettings

#: Fields the Super Admin may PATCH (``last_reminders_date`` is maintained by the reminder run only).
EDITABLE_FIELDS = (
    "company_name",
    "short_name",
    "phone",
    "address",
    "vaccination_reminder_days",
    "expiry_reminder_days",
    "mortality_alert_threshold_pct",
    "problem_report_days",
    "low_stock_alert_enabled",
    "delivery_proof_required",
    "reminder_hour",
)


def get_settings() -> SystemSettings:
    """The singleton row (created on first use if the data migration was skipped). One query."""
    row = SystemSettings.objects.select_related("updated_by").filter(pk=1).first()
    if row is None:
        row, _ = SystemSettings.objects.get_or_create(pk=1)
    return row


def lock_settings() -> SystemSettings:
    """``select_for_update()`` on the singleton (first in the lock order, V3 §17.1). Call inside atomic()."""
    get_settings()  # make sure the row exists
    return SystemSettings.objects.select_for_update().get(pk=1)


def update_settings(changes: dict, *, actor: User) -> SystemSettings:
    unknown = set(changes) - set(EDITABLE_FIELDS)
    if unknown:
        raise ValueError(f"Not editable: {sorted(unknown)}")
    with transaction.atomic():
        row = lock_settings()
        before = audit.snapshot(row)
        for name, value in changes.items():
            setattr(row, name, value)
        row.updated_by = actor
        row.save()
        old, new = audit.diff(before, audit.snapshot(row))
        if new:
            audit.record(
                module=AuditModule.SETTINGS, action=AuditAction.UPDATE, obj=row, actor=actor, before=old, after=new
            )
    return get_settings()

"""Notification queue. Rows are created with `transaction.on_commit` (only after the action is saved)
and with an idempotent `event_key` per user (the same event is never queued twice)."""

from html import escape

from django.db import transaction
from django.utils import timezone

from .models import Notification

TITLES = {
    "active": "🐔 NNP — YANGI VAZIFA",
    "submitted": "📝 NNP — TASDIQLASH KUTILMOQDA",
    "returned": "↩️ NNP — QAYTARILDI",
    "rejected": "⛔ NNP — XONADON RAD ETILDI",
    "done": "✅ NNP — XONADON YAKUNLANDI",
    "overdue": "⏰ NNP — MUDDAT O‘TDI",
}


def compose(kind: str, case, stage_number: int, *, note: str = "", due_at=None) -> str:
    from cases import stages as chain

    stage = chain.get(stage_number)
    lines = [
        f"<b>{TITLES[kind]}</b>",
        "",
        f"<b>Xonadon:</b> {escape(case.code)} — {escape(case.name)}",
        f"<b>Bosqich:</b> {stage.number}. {escape(stage.title)}",
    ]
    if due_at:
        lines.append(f"<b>Muddat:</b> {timezone.localtime(due_at):%d.%m.%Y %H:%M}")
    if note:
        lines.append(f"<b>Izoh:</b> {escape(note[:500])}")
    lines += ["", f"Operatsiya ID: #{escape(case.code)}-{stage.number}"]
    return "\n".join(lines)


def queue(users, *, text: str, button_path: str, event_key: str) -> None:
    """Queue `text` for each user once the current transaction commits."""
    user_ids = sorted({user.pk for user in users if user is not None})

    def create():
        Notification.objects.bulk_create(
            [Notification(user_id=uid, text=text, button_path=button_path, event_key=f"{event_key}:u{uid}"[:120])
             for uid in user_ids],
            ignore_conflicts=True,
        )

    if user_ids:
        transaction.on_commit(create)


def pending_for_bot(limit: int):
    return (
        Notification.objects.filter(
            status=Notification.PENDING, user__telegram_id__isnull=False, user__is_active=True
        )
        .select_related("user")
        .order_by("id")[:limit]
    )


@transaction.atomic
def record_result(notification_id: int, *, ok: bool, error: str = "") -> Notification:
    notification = Notification.objects.select_for_update(of=("self",)).get(pk=notification_id)
    if notification.status != Notification.PENDING:
        return notification
    notification.attempts += 1
    if ok:
        notification.status = Notification.SENT
        notification.sent_at = timezone.now()
        notification.last_error = ""
    else:
        notification.last_error = (error or "")[:2000]
        if notification.attempts >= Notification.MAX_ATTEMPTS:
            notification.status = Notification.FAILED
    notification.save(update_fields=["status", "sent_at", "attempts", "last_error"])
    return notification

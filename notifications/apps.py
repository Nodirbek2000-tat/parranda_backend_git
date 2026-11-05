from django.apps import AppConfig


class NotificationsConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "notifications"
    verbose_name = "Bildirishnomalar"

    def ready(self) -> None:
        from audit import services as audit
        from audit.models import AuditObjectType

        from .models import Notification, NotificationPreference, NotificationSetting

        audit.register_snapshot(NotificationSetting, AuditObjectType.NOTIFICATION_SETTING, _setting_snapshot)
        audit.register_snapshot(
            NotificationPreference, AuditObjectType.NOTIFICATION_PREFERENCE, _preference_snapshot
        )
        audit.register_snapshot(
            Notification, AuditObjectType.NOTIFICATION, _notification_snapshot, op_id_attr="related_op"
        )


def _setting_snapshot(row) -> dict:
    return {
        "event": row.event,
        "enabled": row.enabled,
        "telegram_enabled": row.telegram_enabled,
        "roles": row.roles,
        "contextual_enabled": row.contextual_enabled,
    }


def _preference_snapshot(row) -> dict:
    return {"event": row.event, "enabled": row.enabled, "telegram_enabled": row.telegram_enabled}


def _notification_snapshot(row) -> dict:
    return {
        "event": row.event,
        "recipient_id": row.recipient_id,
        "status": row.status,
        "attempts": row.attempts,
        "related_op": row.related_op,
    }

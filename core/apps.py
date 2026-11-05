from django.apps import AppConfig


class CoreConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "core"
    verbose_name = "Umumiy: sozlamalar va fayllar"

    def ready(self) -> None:
        from audit import services as audit
        from audit.models import AuditObjectType

        from .models import Attachment, SystemSettings

        audit.register_snapshot(SystemSettings, AuditObjectType.SYSTEM_SETTINGS, _settings_snapshot)
        audit.register_snapshot(Attachment, AuditObjectType.ATTACHMENT, _attachment_snapshot)


def _settings_snapshot(row) -> dict:
    from .settings_service import EDITABLE_FIELDS

    return {name: getattr(row, name) for name in EDITABLE_FIELDS}


def _attachment_snapshot(row) -> dict:
    # No storage path and no bytes (API_CONTRACT_V3 §13).
    return {
        "name": row.original_name,
        "content_type": row.content_type,
        "size": row.size,
        "sha256": row.sha256,
        "purpose": row.purpose,
        "object_type": row.object_type,
        "object_id": row.object_id,
        "household_id": row.household_id,
    }

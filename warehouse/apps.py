from django.apps import AppConfig


class WarehouseConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "warehouse"
    verbose_name = "Ombor"

    def ready(self) -> None:
        from audit import services as audit
        from audit.models import AuditObjectType
        from notifications import reminders as reminder_registry

        from . import reminders
        from .models import InventoryCount

        audit.register_snapshot(
            InventoryCount,
            AuditObjectType.INVENTORY_COUNT,
            lambda count: {
                "code": count.code,
                "status": count.status,
                "note": count.note,
                "created_by_id": count.created_by_id,
            },
            op_id_attr="code",
        )
        # V3 §16.1 step 5 (after the vet jobs: vaccination 10–30, visits 40).
        reminder_registry.register_job("lot_expiring", reminders.lot_expiring_job, order=50)
        reminder_registry.register_job("lot_expired", reminders.lot_expired_job, order=51)

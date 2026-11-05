from django.apps import AppConfig


class LogisticsConfig(AppConfig):
    """Logistics (API_CONTRACT_V3 §7): Vehicle, Driver, LogisticsTask, LogisticsTaskItem, LogisticsTaskStatusLog."""

    default_auto_field = "django.db.models.BigAutoField"
    name = "logistics"
    verbose_name = "Logistika"

    def ready(self) -> None:
        from audit import services as audit
        from audit.models import AuditObjectType
        from common.roles import Role
        from core import search

        from . import selectors
        from .models import Driver, LogisticsTask, Vehicle

        audit.register_snapshot(
            LogisticsTask,
            AuditObjectType.LOGISTICS_TASK,
            lambda task: {
                "code": task.code,
                "status": task.status,
                "order_id": task.order_id,
                "household_id": task.household_id,
                "planned_date": task.planned_date,
                "vehicle_id": task.vehicle_id,
                "driver_id": task.driver_id,
                "responsible_id": task.responsible_id,
                "address": task.address_text,
                "note": task.note,
                "items": [
                    {"product_id": item.product_id, "quantity": item.quantity}
                    for item in task.items.order_by("id")
                ]
                if task.pk
                else [],
            },
            op_id_attr="code",
        )
        audit.register_snapshot(
            Vehicle,
            AuditObjectType.VEHICLE,
            lambda vehicle: {
                "plate_number": vehicle.plate_number,
                "model": vehicle.model,
                "capacity_kg": vehicle.capacity_kg,
                "is_active": vehicle.is_active,
                "note": vehicle.note,
            },
        )
        audit.register_snapshot(
            Driver,
            AuditObjectType.DRIVER,
            lambda driver: {
                "full_name": driver.full_name,
                "phone": driver.phone,
                "license_no": driver.license_no,
                "user_id": driver.user_id,
                "is_active": driver.is_active,
                "note": driver.note,
            },
        )
        search.register_group(
            "task",
            "Logistika topshiriqlari",
            {Role.ADMIN, Role.WAREHOUSE, Role.LOGISTICS, Role.OWNER},
            selectors.search_tasks,
            order=50,
        )

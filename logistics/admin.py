from django.contrib import admin

from common.admin import ReadOnlyAdmin

from .models import Driver, LogisticsTask, Vehicle


# Fleet is edited only in the Web App (/api/logistics/vehicles|drivers): plate normalization, the
# "driver user = active LOGISTICS" rule and the audit trail live in the services, not in the admin.
@admin.register(Vehicle)
class VehicleAdmin(ReadOnlyAdmin):
    list_display = ("plate_number", "model", "capacity_kg", "is_active")
    search_fields = ("plate_number", "model")


@admin.register(Driver)
class DriverAdmin(ReadOnlyAdmin):
    list_display = ("full_name", "phone", "license_no", "user", "is_active")
    search_fields = ("full_name", "phone")


@admin.register(LogisticsTask)
class LogisticsTaskAdmin(admin.ModelAdmin):
    """Read-only: tasks change only through the services (status machine, stock, audit)."""

    list_display = ("code", "status", "household", "planned_date", "vehicle", "driver")
    list_filter = ("status",)
    search_fields = ("code", "household__name")

    def has_add_permission(self, request) -> bool:
        return False

    def has_change_permission(self, request, obj=None) -> bool:
        return False

    def has_delete_permission(self, request, obj=None) -> bool:
        return False

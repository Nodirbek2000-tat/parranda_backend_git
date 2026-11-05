from django.contrib import admin

from audit.admin import AuditedAdminMixin
from audit.models import AuditModule
from common.admin import ReadOnlyAdmin

from .models import DailyReport, Household, PoultryBatch, PoultryMovement


@admin.register(Household)
class HouseholdAdmin(AuditedAdminMixin, admin.ModelAdmin):
    """Households are created through the API (owner account + notification); editable here."""

    audit_module = AuditModule.HOUSEHOLDS
    list_display = ("code", "name", "owner", "phone", "region", "district", "is_active")
    list_filter = ("is_active", "region", "district")
    search_fields = ("name", "phone", "mfy", "address", "owner__username", "owner__last_name")
    readonly_fields = ("owner", "created_at", "updated_at")
    list_select_related = ("owner",)

    def has_add_permission(self, request) -> bool:
        return False

    def has_delete_permission(self, request, obj=None) -> bool:
        return False

    @admin.display(description="Kodi")
    def code(self, obj: Household) -> str:
        return obj.code


@admin.register(PoultryBatch)
class PoultryBatchAdmin(AuditedAdminMixin, admin.ModelAdmin):
    """Batches are created through the API (code sequence); quantities are never edited."""

    audit_module = AuditModule.BATCHES
    list_display = ("code", "poultry_type", "breed", "initial_quantity", "arrived_at", "supplier")
    list_filter = ("poultry_type",)
    search_fields = ("code", "breed", "supplier")
    readonly_fields = ("code", "year_seq", "initial_quantity", "arrived_at", "created_by", "created_at")

    def has_add_permission(self, request) -> bool:
        return False

    def has_delete_permission(self, request, obj=None) -> bool:
        return False


@admin.register(PoultryMovement)
class PoultryMovementAdmin(ReadOnlyAdmin):
    list_display = ("op_id", "type", "batch", "household", "quantity", "date", "created_by", "created_at")
    list_filter = ("type", "date")
    search_fields = ("batch__code", "household__name", "note")
    list_select_related = ("batch", "household", "created_by")
    date_hierarchy = "date"


@admin.register(DailyReport)
class DailyReportAdmin(ReadOnlyAdmin):
    list_display = ("household", "date", "bird_count", "meat_kg", "feed_kg", "avg_weight_g", "deaths", "created_by")
    list_filter = ("date",)
    search_fields = ("household__name",)
    list_select_related = ("household", "created_by")
    date_hierarchy = "date"

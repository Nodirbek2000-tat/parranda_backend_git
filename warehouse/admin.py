from django.contrib import admin

from audit.admin import AuditedAdminMixin
from audit.models import AuditModule
from common.admin import ReadOnlyAdmin

from .models import InventoryCount, Product, StockLot, StockTransaction


@admin.register(Product)
class ProductAdmin(AuditedAdminMixin, admin.ModelAdmin):
    audit_module = AuditModule.PRODUCTS
    list_display = ("name", "category", "unit", "balance", "min_stock", "is_active")
    list_filter = ("category", "unit", "is_active")
    search_fields = ("name",)
    readonly_fields = ("balance", "created_at")

    def has_delete_permission(self, request, obj=None) -> bool:
        return False


@admin.register(StockTransaction)
class StockTransactionAdmin(ReadOnlyAdmin):
    list_display = ("op_id", "type", "product", "quantity", "balance_after", "household", "document_no", "created_at")
    list_filter = ("type", "product__category")
    search_fields = ("product__name", "document_no", "household__name")
    list_select_related = ("product", "household")


@admin.register(StockLot)
class StockLotAdmin(ReadOnlyAdmin):
    list_display = ("id", "product", "lot_no", "expiry_date", "received_qty", "remaining_qty", "created_at")
    list_filter = ("product__category",)
    search_fields = ("product__name", "lot_no")
    list_select_related = ("product",)


@admin.register(InventoryCount)
class InventoryCountAdmin(ReadOnlyAdmin):
    list_display = ("code", "status", "created_by", "created_at", "approved_at")
    list_filter = ("status",)

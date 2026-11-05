"""Django admin: read-only views of orders (every change goes through ``orders.services`` → audit log)."""

from django.contrib import admin

from .models import Order, OrderItem, OrderStatusLog


class _ReadOnly:
    def has_add_permission(self, request, obj=None) -> bool:
        return False

    def has_change_permission(self, request, obj=None) -> bool:
        return False

    def has_delete_permission(self, request, obj=None) -> bool:
        return False


class OrderItemInline(_ReadOnly, admin.TabularInline):
    model = OrderItem
    extra = 0
    fields = ("product", "requested_qty", "approved_qty", "note")
    readonly_fields = fields


class OrderStatusLogInline(_ReadOnly, admin.TabularInline):
    model = OrderStatusLog
    extra = 0
    fields = ("created_at", "from_status", "to_status", "actor_name", "note")
    readonly_fields = fields


@admin.register(Order)
class OrderAdmin(_ReadOnly, admin.ModelAdmin):
    list_display = ("code", "kind", "household", "status", "created_at")
    list_filter = ("status", "kind")
    search_fields = ("household__name",)
    list_select_related = ("household",)
    inlines = (OrderItemInline, OrderStatusLogInline)

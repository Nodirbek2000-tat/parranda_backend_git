from django.contrib import admin

from .models import Package, PackageItem, Product, StockMove


@admin.register(Product)
class ProductAdmin(admin.ModelAdmin):
    list_display = ("name", "category", "unit", "price", "balance", "min_stock", "is_demo")
    list_filter = ("category", "is_demo")
    search_fields = ("name",)
    readonly_fields = ("balance",)  # changes only through stock moves (API)


@admin.register(StockMove)
class StockMoveAdmin(admin.ModelAdmin):
    list_display = ("created_at", "product", "type", "qty", "balance_after", "case", "created_by")
    list_filter = ("type",)

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False


class PackageItemInline(admin.TabularInline):
    model = PackageItem
    extra = 0


@admin.register(Package)
class PackageAdmin(admin.ModelAdmin):
    list_display = ("name", "area_sotix", "capacity_birds", "price", "is_demo")
    inlines = [PackageItemInline]

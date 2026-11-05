from django.contrib import admin


class ReadOnlyAdmin(admin.ModelAdmin):
    """Immutable operation log: visible in the admin, never added, changed or deleted there."""

    def has_add_permission(self, request, obj=None) -> bool:
        return False

    def has_change_permission(self, request, obj=None) -> bool:
        return False

    def has_delete_permission(self, request, obj=None) -> bool:
        return False

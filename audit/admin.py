from __future__ import annotations

from django.contrib import admin
from django.db import transaction

from . import services
from .models import AuditLog


class AuditedAdminMixin:
    """Logs Django admin create/change of a model (source ``ADMIN``) in the same transaction."""

    audit_module: str = ""

    def save_model(self, request, obj, form, change) -> None:
        with transaction.atomic():
            before = None
            if change and obj.pk is not None:
                stored = type(obj)._default_manager.filter(pk=obj.pk).first()
                before = services.snapshot(stored) if stored is not None else None
            super().save_model(request, obj, form, change)
            if before is None:
                services.record_create(self.audit_module, obj, request.user)
            else:
                services.record_update(self.audit_module, obj, request.user, before)


@admin.register(AuditLog)
class AuditLogAdmin(admin.ModelAdmin):
    """Read-only for everybody, superusers included (API_CONTRACT_V2 §2.1)."""

    list_display = ("created_at", "actor_name", "role", "source", "module", "action", "object_repr", "op_id", "ip")
    list_filter = ("source", "module", "action", "object_type")
    search_fields = ("actor_name", "object_repr", "op_id", "ip")
    list_select_related = ("actor",)
    date_hierarchy = "created_at"
    readonly_fields = [field.name for field in AuditLog._meta.fields]

    def has_add_permission(self, request, obj=None) -> bool:
        return False

    def has_change_permission(self, request, obj=None) -> bool:
        return False

    def has_delete_permission(self, request, obj=None) -> bool:
        return False

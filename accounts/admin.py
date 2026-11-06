from django.contrib import admin
from django.contrib.auth.admin import UserAdmin as BaseUserAdmin

from .models import User


@admin.register(User)
class UserAdmin(BaseUserAdmin):
    list_display = ("username", "full_name", "role", "phone", "telegram_id", "is_active", "deleted_at")
    list_filter = ("role", "is_active")
    search_fields = ("username", "full_name", "phone")
    fieldsets = BaseUserAdmin.fieldsets + (
        ("NNP", {"fields": ("role", "full_name", "phone", "telegram_id", "must_change_password", "token_version",
                            "deleted_at")}),
    )
    add_fieldsets = BaseUserAdmin.add_fieldsets + (("NNP", {"fields": ("role", "full_name", "phone")}),)

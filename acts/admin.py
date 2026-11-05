from django.contrib import admin

from common.admin import ReadOnlyAdmin

from .models import Act


@admin.register(Act)
class ActAdmin(ReadOnlyAdmin):
    list_display = ("code", "kind", "role", "user", "household", "date_from", "date_to", "created_by", "created_at")
    list_filter = ("kind", "role")
    list_select_related = ("user", "household", "created_by")

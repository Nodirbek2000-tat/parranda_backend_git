from django.contrib import admin

from common.admin import ReadOnlyAdmin

from .models import Notification, NotificationSetting


@admin.register(Notification)
class NotificationAdmin(ReadOnlyAdmin):
    list_display = ("id", "event", "recipient", "status", "attempts", "related_op", "created_at", "sent_at", "read_at")
    list_filter = ("status", "event")
    search_fields = ("event_key", "related_op", "recipient__username")
    list_select_related = ("recipient",)


@admin.register(NotificationSetting)
class NotificationSettingAdmin(ReadOnlyAdmin):
    """Edited only by the Super Admin in the Web App (``/settings``), so every change is audited there."""

    list_display = ("event", "enabled", "telegram_enabled", "roles", "contextual_enabled", "updated_by", "updated_at")
    list_select_related = ("updated_by",)

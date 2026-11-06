from django.contrib import admin

from .models import Notification


@admin.register(Notification)
class NotificationAdmin(admin.ModelAdmin):
    list_display = ("id", "user", "status", "attempts", "created_at", "sent_at", "read_at")
    list_filter = ("status",)
    search_fields = ("event_key", "user__username")

from django.contrib import admin

from common.admin import ReadOnlyAdmin

from .models import Attachment, SystemSettings


@admin.register(SystemSettings)
class SystemSettingsAdmin(ReadOnlyAdmin):
    """Read-only: settings change only through ``PATCH /api/settings`` (bounds validation + audit, V3 §3)."""

    list_display = ("company_name", "short_name", "phone", "updated_at", "updated_by")


@admin.register(Attachment)
class AttachmentAdmin(ReadOnlyAdmin):
    list_display = ("id", "original_name", "purpose", "content_type", "size", "uploaded_by", "object_type",
                    "object_id", "created_at")
    list_filter = ("purpose", "content_type", "object_type")
    search_fields = ("original_name", "sha256")
    list_select_related = ("uploaded_by",)
    exclude = ("file",)  # never expose the storage path / a media link in the admin

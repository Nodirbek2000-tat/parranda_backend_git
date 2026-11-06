from django.contrib import admin

from .models import Case, CaseStage, Document, Photo, StageEvent


class StageInline(admin.TabularInline):
    model = CaseStage
    extra = 0
    fields = ("number", "key", "status", "activated_at", "due_at", "submitted_by", "decided_by", "return_reason")
    readonly_fields = fields
    can_delete = False


@admin.register(Case)
class CaseAdmin(admin.ModelAdmin):
    list_display = ("code", "name", "owner_full_name", "district", "status", "current_stage", "created_at")
    list_filter = ("status", "current_stage", "region")
    search_fields = ("code", "name", "owner_full_name", "owner_phone")
    raw_id_fields = ("owner_user", "created_by")
    inlines = [StageInline]


@admin.register(StageEvent)
class StageEventAdmin(admin.ModelAdmin):
    list_display = ("case", "stage_number", "action", "actor", "created_at")
    list_filter = ("action", "stage_number")


@admin.register(Photo)
class PhotoAdmin(admin.ModelAdmin):
    list_display = ("id", "case", "stage", "live", "taken_at", "uploaded_by")


@admin.register(Document)
class DocumentAdmin(admin.ModelAdmin):
    list_display = ("id", "case", "stage", "original_name", "uploaded_by")

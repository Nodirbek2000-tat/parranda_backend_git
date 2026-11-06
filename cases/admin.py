from django.contrib import admin

from .models import Case, CaseStage, Document, Photo, Request, StageEvent, VetVisit, VisitDrug


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
    raw_id_fields = ("owner_user", "created_by", "vet_user")
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


@admin.register(Request)
class RequestAdmin(admin.ModelAdmin):
    list_display = ("id", "case", "kind", "status", "qty_kg", "created_by", "created_at")
    list_filter = ("kind", "status")
    raw_id_fields = ("case", "created_by", "logistics_by", "admin_by", "delivered_by", "done_by", "rejected_by",
                     "delivered_product")


class VisitDrugInline(admin.TabularInline):
    model = VisitDrug
    extra = 0
    raw_id_fields = ("product",)


@admin.register(VetVisit)
class VetVisitAdmin(admin.ModelAdmin):
    list_display = ("id", "case", "vet", "kind", "week", "planned_date", "status")
    list_filter = ("kind", "status")
    raw_id_fields = ("case", "vet", "request", "cancelled_by")
    inlines = [VisitDrugInline]

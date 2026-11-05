"""Django admin registrations of the vet app (read-only: every change goes through the API services)."""

from django.contrib import admin

from common.admin import ReadOnlyAdmin

from .models import DiseaseRecord, Treatment, Vaccination, VetRequest, VetVisit


@admin.register(VetVisit)
class VetVisitAdmin(ReadOnlyAdmin):
    list_display = ("id", "household", "planned_date", "status", "vet", "health_status")
    list_filter = ("status", "health_status")
    search_fields = ("purpose", "household__name")


@admin.register(Vaccination)
class VaccinationAdmin(ReadOnlyAdmin):
    list_display = ("id", "household", "vaccine_name", "planned_date", "status", "vet")
    list_filter = ("status",)
    search_fields = ("vaccine_name", "household__name")


@admin.register(DiseaseRecord)
class DiseaseRecordAdmin(ReadOnlyAdmin):
    list_display = ("id", "household", "disease_name", "diagnosed_date", "cases", "status")
    list_filter = ("status",)
    search_fields = ("disease_name", "household__name")


@admin.register(Treatment)
class TreatmentAdmin(ReadOnlyAdmin):
    list_display = ("id", "household", "drug_name", "start_date", "end_date", "vet")
    search_fields = ("drug_name", "household__name")


@admin.register(VetRequest)
class VetRequestAdmin(ReadOnlyAdmin):
    list_display = ("id", "household", "status", "vet", "created_at")
    list_filter = ("status",)
    search_fields = ("text", "household__name")

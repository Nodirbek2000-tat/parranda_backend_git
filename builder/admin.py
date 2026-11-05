"""Read-mostly Django admin of the Konstruktor (changes go through the API: validation, versions, audit)."""

from django.contrib import admin

from .models import Blueprint, ConfigVersion, FormSubmission, ProcessRun, TaskInstance, UserAccess


class _ReadOnly(admin.ModelAdmin):
    def has_add_permission(self, request) -> bool:
        return False

    def has_change_permission(self, request, obj=None) -> bool:
        return False

    def has_delete_permission(self, request, obj=None) -> bool:
        return False


@admin.register(Blueprint)
class BlueprintAdmin(_ReadOnly):
    list_display = ("id", "kind", "name", "status", "version", "updated_at")
    list_filter = ("kind", "status")
    search_fields = ("name",)


@admin.register(ConfigVersion)
class ConfigVersionAdmin(_ReadOnly):
    list_display = ("object_type", "object_id", "version", "name", "status", "created_by", "created_at")
    list_filter = ("object_type",)


@admin.register(UserAccess)
class UserAccessAdmin(_ReadOnly):
    list_display = ("user", "profile", "updated_at")


@admin.register(TaskInstance)
class TaskInstanceAdmin(_ReadOnly):
    list_display = ("code", "title", "executor", "approver", "status", "household", "due_on", "is_test")
    list_filter = ("status", "is_test")


@admin.register(ProcessRun)
class ProcessRunAdmin(_ReadOnly):
    list_display = ("code", "process", "household", "current_index", "status", "is_test")
    list_filter = ("status", "is_test")


@admin.register(FormSubmission)
class FormSubmissionAdmin(_ReadOnly):
    list_display = ("code", "form", "household", "task", "submitted_by", "created_at", "is_test")
    list_filter = ("is_test",)

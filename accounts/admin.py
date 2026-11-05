from django.contrib import admin
from django.contrib.auth.admin import UserAdmin as BaseUserAdmin
from django.contrib.auth.forms import AdminPasswordChangeForm
from django.db import transaction

from audit import services as audit
from audit.admin import AuditedAdminMixin
from audit.models import AuditAction, AuditModule

from . import tokens
from .models import User

admin.site.site_header = "NNP — Navobod naslli parranda qisqarmasi"
admin.site.site_title = "NNP boshqaruvi"
admin.site.index_title = "Ma’lumotlar boshqaruvi"


class AuditedAdminPasswordChangeForm(AdminPasswordChangeForm):
    """Admin password change: revokes the user's sessions and writes a ``password_reset`` audit row."""

    def save(self, commit: bool = True) -> User:
        if not commit:
            return super().save(commit=False)
        with transaction.atomic():
            user = super().save(commit=False)
            tokens.bump_token_version(user)
            user.save()
            audit.record(
                module=AuditModule.USERS,
                action=AuditAction.PASSWORD_RESET,
                obj=user,
                actor=audit.context_user(),
                after={"must_change_password": user.must_change_password},
            )
        return user


@admin.register(User)
class UserAdmin(AuditedAdminMixin, BaseUserAdmin):
    audit_module = AuditModule.USERS
    change_password_form = AuditedAdminPasswordChangeForm
    list_display = ("username", "full_name", "role", "phone", "telegram_id", "is_active", "is_superuser")
    list_filter = ("role", "is_active", "is_superuser", "must_change_password")
    search_fields = ("username", "first_name", "last_name", "phone", "telegram_id")
    ordering = ("id",)
    fieldsets = (
        *BaseUserAdmin.fieldsets,
        ("NNP", {"fields": ("role", "phone", "telegram_id", "must_change_password")}),
    )
    add_fieldsets = (
        *BaseUserAdmin.add_fieldsets,
        ("NNP", {"fields": ("role", "phone", "first_name", "last_name")}),
    )

    @admin.display(description="F.I.Sh.")
    def full_name(self, obj: User) -> str:
        return obj.full_name

    def has_delete_permission(self, request, obj=None) -> bool:
        """Accounts are never deleted here (no trace would remain): block them with ``is_active`` instead."""
        return False

    @staticmethod
    def _access_snapshot(user: User) -> dict[str, list[str]]:
        """Group names and ``app_label.codename`` of the direct permissions (m2m, saved in ``save_related``)."""
        if user.pk is None:
            return {"groups": [], "user_permissions": []}
        return {
            "groups": sorted(user.groups.values_list("name", flat=True)),
            "user_permissions": sorted(
                f"{app_label}.{codename}"
                for app_label, codename in user.user_permissions.values_list("content_type__app_label", "codename")
            ),
        }

    def save_related(self, request, form, formsets, change) -> None:
        """Django-admin model permissions / groups are part of the audit trail (``users`` / ``update``)."""
        user: User = form.instance
        with transaction.atomic():
            before = self._access_snapshot(user) if change else {"groups": [], "user_permissions": []}
            super().save_related(request, form, formsets, change)
            after = self._access_snapshot(user)
            changed = [key for key in after if after[key] != before[key]]
            if changed:
                audit.record(
                    module=AuditModule.USERS,
                    action=AuditAction.UPDATE,
                    obj=user,
                    actor=request.user,
                    before={key: before[key] for key in changed},
                    after={key: after[key] for key in changed},
                )

    def save_model(self, request, obj, form, change) -> None:
        """Profile changes are logged by the mixin; a Telegram id edited here is logged as link/unlink."""
        old_telegram_id = (
            User.objects.filter(pk=obj.pk).values_list("telegram_id", flat=True).first() if change else None
        )
        with transaction.atomic():
            super().save_model(request, obj, form, change)
            if obj.telegram_id != old_telegram_id:
                linked = obj.telegram_id is not None
                audit.record(
                    module=AuditModule.TELEGRAM,
                    action=AuditAction.LINK if linked else AuditAction.UNLINK,
                    obj=obj,
                    actor=request.user,
                    before={"telegram_id": old_telegram_id},
                    after={"telegram_id": obj.telegram_id},
                )

from django.conf import settings
from django.db import models


class AuditLog(models.Model):
    """Immutable record of who changed what. Rows are never updated or deleted through the ORM."""

    actor = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL)
    action = models.CharField(max_length=60)
    object_type = models.CharField(max_length=40)
    object_id = models.CharField(max_length=40, blank=True)
    before = models.JSONField(null=True, blank=True)
    after = models.JSONField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        ordering = ["-id"]
        verbose_name = "Audit yozuvi"
        verbose_name_plural = "Audit jurnali"

    def save(self, *args, **kwargs):
        if self.pk is not None:
            raise ValueError("AuditLog is immutable")
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValueError("AuditLog is immutable")

    def __str__(self) -> str:
        return f"{self.created_at:%Y-%m-%d %H:%M} {self.action} {self.object_type}#{self.object_id}"


def log(actor, action: str, obj=None, *, object_type: str = "", object_id="", before=None, after=None) -> AuditLog:
    if obj is not None:
        object_type = object_type or obj._meta.model_name
        object_id = object_id or obj.pk
    if actor is not None and not getattr(actor, "is_authenticated", False):
        actor = None
    return AuditLog.objects.create(
        actor=actor, action=action, object_type=object_type, object_id=str(object_id or ""),
        before=before, after=after,
    )

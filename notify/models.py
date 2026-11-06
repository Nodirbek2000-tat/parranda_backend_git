from django.conf import settings
from django.db import models


class Notification(models.Model):
    PENDING = "PENDING"
    SENT = "SENT"
    FAILED = "FAILED"
    STATUSES = [(PENDING, "Kutilmoqda"), (SENT, "Yuborildi"), (FAILED, "Yuborilmadi")]
    MAX_ATTEMPTS = 5

    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="notifications")
    text = models.TextField()  # Telegram HTML
    button_path = models.CharField(max_length=200, blank=True)
    status = models.CharField(max_length=10, choices=STATUSES, default=PENDING, db_index=True)
    attempts = models.PositiveSmallIntegerField(default=0)
    last_error = models.TextField(blank=True)
    # One row per (event, user): the same event is never queued twice.
    event_key = models.CharField(max_length=120, unique=True)
    created_at = models.DateTimeField(auto_now_add=True)
    sent_at = models.DateTimeField(null=True, blank=True)
    read_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-id"]
        verbose_name = "Bildirishnoma"
        verbose_name_plural = "Bildirishnomalar"

    def __str__(self) -> str:
        return f"#{self.pk} → {self.user} [{self.status}]"

from django.conf import settings
from django.db import models
from django.db.models import Q


class NotificationStatus(models.TextChoices):
    PENDING = "PENDING", "Navbatda"
    SENT = "SENT", "Yuborildi"
    FAILED = "FAILED", "Yuborilmadi"
    #: In-app (bell) only: the recipient has no Telegram link or Telegram is off for the event.
    SKIPPED = "SKIPPED", "Telegramga yuborilmaydi"


class Notification(models.Model):
    """One row per recipient: a Telegram message for the bot (``PENDING``) and the Web App inbox item.

    ``event_key`` (``"{event}:{key}:{user_id}"``) makes queuing idempotent. Rows are never deleted by the API.
    """

    MAX_ATTEMPTS = 5

    recipient = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="notifications",
        verbose_name="Qabul qiluvchi",
    )
    #: Telegram chat at queue time; null for ``SKIPPED`` rows (constraint: PENDING ⇒ not null).
    chat_id = models.BigIntegerField("Chat ID", null=True, blank=True)
    event = models.CharField("Hodisa", max_length=50)
    event_key = models.CharField("Hodisa kaliti", max_length=200, unique=True)
    text = models.TextField("Matn (HTML)")
    #: Plain text title without emoji/HTML, e.g. ``NNP — PARRANDA TARQATILDI``.
    title = models.CharField("Sarlavha", max_length=200, blank=True)
    #: Plain text body (``text`` without tags and the title line).
    body = models.TextField("Matn", blank=True)
    button_path = models.CharField("Tugma yo‘li", max_length=200, blank=True)
    related_op = models.CharField("Operatsiya", max_length=40, blank=True)
    object_type = models.CharField("Obyekt turi", max_length=32, blank=True)
    object_id = models.BigIntegerField("Obyekt ID", null=True, blank=True)
    status = models.CharField(
        "Holat", max_length=16, choices=NotificationStatus.choices, default=NotificationStatus.PENDING
    )
    attempts = models.PositiveSmallIntegerField("Urinishlar", default=0)
    last_error = models.TextField("Oxirgi xato", blank=True)
    created_at = models.DateTimeField("Yaratilgan", auto_now_add=True)
    sent_at = models.DateTimeField("Yuborilgan", null=True, blank=True)
    #: Read in the Web App bell (does not change the Telegram delivery status).
    read_at = models.DateTimeField("O‘qilgan", null=True, blank=True)
    #: V4 §4 delayed delivery: the bot queue skips the row until this moment (null = now).
    send_after = models.DateTimeField("Yuborish vaqti", null=True, blank=True)

    class Meta:
        verbose_name = "Bildirishnoma"
        verbose_name_plural = "Bildirishnomalar"
        ordering = ("-id",)
        indexes = [
            models.Index(fields=["status", "attempts", "id"]),
            models.Index(fields=["event"]),
            models.Index(fields=["recipient", "read_at", "id"], name="notif_inbox_idx"),
            models.Index(fields=["status", "created_at"], name="notif_status_created_idx"),
            models.Index(fields=["event", "created_at"], name="notif_event_created_idx"),
        ]
        constraints = [
            models.CheckConstraint(
                condition=Q(status=NotificationStatus.SENT, sent_at__isnull=False)
                | ~Q(status=NotificationStatus.SENT),
                name="notification_sent_has_sent_at",
            ),
            models.CheckConstraint(
                condition=~Q(status=NotificationStatus.PENDING) | Q(chat_id__isnull=False),
                name="notification_pending_has_chat_id",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.event} → {self.recipient_id} [{self.status}]"


class NotificationSetting(models.Model):
    """Per-event switches edited by the Super Admin (API_CONTRACT_V3 §9.3). Missing row = defaults."""

    event = models.CharField("Hodisa", max_length=50, unique=True)
    enabled = models.BooleanField("Yoqilgan", default=True)
    telegram_enabled = models.BooleanField("Telegram orqali", default=True)
    #: ``None`` = registry default roles; otherwise a list ⊆ ADMIN, WAREHOUSE, LOGISTICS, VET.
    roles = models.JSONField("Rollar", null=True, blank=True)
    contextual_enabled = models.BooleanField("Kontekst bo‘yicha qabul qiluvchilar", default=True)
    # API_CONTRACT_V4 §4 (notification constructor).
    #: Replaces the default body; placeholders ``{title} {code} {household} {due} {role} {text}``.
    custom_text = models.TextField("Maxsus matn", blank=True, default="")
    #: "Qachon yuborish": minutes after the event (0 = immediately, max 7 days).
    delay_minutes = models.PositiveIntegerField("Kechiktirish (daqiqa)", default=0)
    #: Specific users added to the recipients.
    extra_user_ids = models.JSONField("Qo‘shimcha qabul qiluvchilar", default=list, blank=True)
    updated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
        verbose_name="O‘zgartirgan",
    )
    updated_at = models.DateTimeField("O‘zgartirilgan", auto_now=True)

    class Meta:
        verbose_name = "Bildirishnoma sozlamasi"
        verbose_name_plural = "Bildirishnoma sozlamalari"
        ordering = ("event",)

    def __str__(self) -> str:
        return self.event


class NotificationPreference(models.Model):
    """A user's own switch for one event (TZ §11.3 "Bildirishnoma sozlamalarini boshqarish").

    Applied after the Super Admin's ``NotificationSetting``: ``enabled=False`` → no row for this user,
    ``telegram_enabled=False`` → inbox only (``SKIPPED``). Missing row = everything on.
    """

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="notification_preferences",
        verbose_name="Foydalanuvchi",
    )
    event = models.CharField("Hodisa", max_length=50)
    enabled = models.BooleanField("Yoqilgan", default=True)
    telegram_enabled = models.BooleanField("Telegram orqali", default=True)
    updated_at = models.DateTimeField("O‘zgartirilgan", auto_now=True)

    class Meta:
        verbose_name = "Shaxsiy bildirishnoma sozlamasi"
        verbose_name_plural = "Shaxsiy bildirishnoma sozlamalari"
        constraints = [models.UniqueConstraint(fields=["user", "event"], name="notification_pref_user_event")]

    def __str__(self) -> str:
        return f"{self.user_id}: {self.event}"

"""Immutable audit journal (API_CONTRACT_V2 §2)."""

from __future__ import annotations

from django.conf import settings
from django.db import models


class AuditModule(models.TextChoices):
    AUTH = "auth", "Kirish"
    USERS = "users", "Foydalanuvchilar"
    TELEGRAM = "telegram", "Telegram"
    HOUSEHOLDS = "households", "Xonadonlar"
    BATCHES = "batches", "Partiyalar"
    MOVEMENTS = "movements", "Parranda harakatlari"
    DAILY_REPORTS = "daily_reports", "Kunlik hisobotlar"
    PRODUCTS = "products", "Mahsulotlar"
    STOCK = "stock", "Ombor operatsiyalari"
    # V3 (API_CONTRACT_V3 §13)
    ORDERS = "orders", "Buyurtmalar"
    LOGISTICS = "logistics", "Logistika"
    FLEET = "fleet", "Transport va haydovchilar"
    VET = "vet", "Veterinariya"
    INVENTORY = "inventory", "Inventarizatsiya"
    FILES = "files", "Fayllar"
    SETTINGS = "settings", "Sozlamalar"
    NOTIFICATIONS = "notifications", "Bildirishnomalar"
    REPORTS = "reports", "Hisobotlar"


class AuditAction(models.TextChoices):
    LOGIN_SUCCESS = "login_success", "Muvaffaqiyatli kirish"
    LOGIN_FAILURE = "login_failure", "Kirish rad etildi"
    PASSWORD_CHANGE = "password_change", "Parol almashtirildi"
    CREATE = "create", "Yaratildi"
    UPDATE = "update", "O‘zgartirildi"
    PASSWORD_RESET = "password_reset", "Parol tiklandi"
    LINK = "link", "Telegram bog‘landi"
    UNLINK = "unlink", "Telegram uzildi"
    # V3 (API_CONTRACT_V3 §13)
    STATUS_CHANGE = "status_change", "Holat o‘zgartirildi"
    APPROVE = "approve", "Tasdiqlandi"
    LOGOUT = "logout", "Tizimdan chiqish"
    SESSIONS_REVOKED = "sessions_revoked", "Sessiyalar yakunlandi"
    UPLOAD = "upload", "Fayl yuklandi"
    RETRY = "retry", "Qayta yuborildi"
    EXPORT = "export", "Eksport"
    DELETE = "delete", "O‘chirildi"


class AuditSource(models.TextChoices):
    API = "API", "Web App / API"
    BOT = "BOT", "Telegram bot"
    ADMIN = "ADMIN", "Django admin"


class AuditObjectType(models.TextChoices):
    USER = "User", "Foydalanuvchi"
    HOUSEHOLD = "Household", "Xonadon"
    BATCH = "PoultryBatch", "Parranda partiyasi"
    MOVEMENT = "PoultryMovement", "Parranda harakati"
    DAILY_REPORT = "DailyReport", "Kunlik hisobot"
    PRODUCT = "Product", "Mahsulot"
    STOCK_TRANSACTION = "StockTransaction", "Ombor operatsiyasi"
    # V3 (API_CONTRACT_V3 §13)
    ORDER = "Order", "Buyurtma"
    LOGISTICS_TASK = "LogisticsTask", "Logistika topshirig‘i"
    VEHICLE = "Vehicle", "Transport vositasi"
    DRIVER = "Driver", "Haydovchi"
    VET_VISIT = "VetVisit", "Veterinariya tekshiruvi"
    VACCINATION = "Vaccination", "Emlash"
    DISEASE_RECORD = "DiseaseRecord", "Kasallik qaydi"
    TREATMENT = "Treatment", "Davolash"
    VET_REQUEST = "VetRequest", "Veterinarga murojaat"
    INVENTORY_COUNT = "InventoryCount", "Inventarizatsiya"
    ATTACHMENT = "Attachment", "Fayl"
    SYSTEM_SETTINGS = "SystemSettings", "Kompaniya sozlamalari"
    NOTIFICATION_SETTING = "NotificationSetting", "Bildirishnoma sozlamasi"
    NOTIFICATION = "Notification", "Bildirishnoma"
    NOTIFICATION_PREFERENCE = "NotificationPreference", "Shaxsiy bildirishnoma sozlamasi"
    REPORT = "Report", "Hisobot"


class AuditStatus(models.TextChoices):
    """TZ §14 "Operatsiya holati"."""

    SUCCESS = "SUCCESS", "Muvaffaqiyatli"
    FAILED = "FAILED", "Rad etildi"


class AuditLogImmutable(Exception):
    """Audit rows are never changed or deleted."""


class AuditLogQuerySet(models.QuerySet):
    def update(self, **kwargs):
        raise AuditLogImmutable("Audit jurnali o‘zgartirilmaydi")

    def delete(self):
        raise AuditLogImmutable("Audit jurnali o‘chirilmaydi")


class AuditLog(models.Model):
    op_id = models.CharField("Operatsiya ID", max_length=20, blank=True)
    actor = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
        verbose_name="Foydalanuvchi",
    )
    actor_name = models.CharField("Foydalanuvchi (nomi)", max_length=200, blank=True)
    role = models.CharField("Rol", max_length=16, blank=True)
    source = models.CharField("Manba", max_length=8, choices=AuditSource.choices)
    module = models.CharField("Bo‘lim", max_length=32, choices=AuditModule.choices)
    action = models.CharField("Amal", max_length=32, choices=AuditAction.choices)
    object_type = models.CharField("Obyekt turi", max_length=32, choices=AuditObjectType.choices, blank=True)
    object_id = models.BigIntegerField("Obyekt ID", null=True, blank=True)
    object_repr = models.CharField("Obyekt", max_length=200, blank=True)
    before = models.JSONField("Oldin", null=True, blank=True)
    after = models.JSONField("Keyin", null=True, blank=True)
    ip = models.GenericIPAddressField("IP", null=True, blank=True)
    # TZ §14 "Qaysi xonadon yoki mahsulotga tegishli?" — plain ids (no FK: the journal outlives any row).
    household_ref = models.BigIntegerField("Xonadon ID", null=True, blank=True)
    product_ref = models.BigIntegerField("Mahsulot ID", null=True, blank=True)
    status = models.CharField("Holati", max_length=8, choices=AuditStatus.choices, default=AuditStatus.SUCCESS)
    created_at = models.DateTimeField("Vaqt", auto_now_add=True)

    objects = AuditLogQuerySet.as_manager()

    class Meta:
        verbose_name = "Audit yozuvi"
        verbose_name_plural = "Audit jurnali"
        ordering = ("-created_at", "-id")
        indexes = [
            models.Index(fields=["created_at"]),
            models.Index(fields=["module", "created_at"]),
            models.Index(fields=["actor", "created_at"]),
            models.Index(fields=["object_type", "object_id"]),
            models.Index(fields=["household_ref", "created_at"]),
            models.Index(fields=["product_ref", "created_at"]),
        ]

    def save(self, *args, **kwargs) -> None:
        if not self._state.adding:
            raise AuditLogImmutable("Audit jurnali o‘zgartirilmaydi")
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise AuditLogImmutable("Audit jurnali o‘chirilmaydi")

    def __str__(self) -> str:
        return f"{self.get_module_display()} · {self.get_action_display()} · {self.object_repr}"

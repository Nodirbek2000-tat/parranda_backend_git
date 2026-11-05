"""Company settings (singleton) and private file attachments (API_CONTRACT_V3 §3, §4)."""

from __future__ import annotations

import uuid
from decimal import Decimal

from django.conf import settings
from django.db import models
from django.db.models import Q
from django.utils import timezone

from .storage import private_storage


class SystemSettings(models.Model):
    """Singleton ``pk=1`` created by a data migration; read with ``core.settings_service.get_settings()``."""

    company_name = models.CharField("Kompaniya nomi", max_length=200, default="Navobod naslli parranda qisqarmasi")
    short_name = models.CharField("Qisqa nomi", max_length=20, default="NNP")
    phone = models.CharField("Telefon", max_length=32, blank=True, default="")
    address = models.CharField("Manzil", max_length=255, blank=True, default="")
    vaccination_reminder_days = models.PositiveSmallIntegerField("Emlash eslatmasi (kun oldin)", default=3)
    expiry_reminder_days = models.PositiveSmallIntegerField("Yaroqlilik eslatmasi (kun oldin)", default=30)
    mortality_alert_threshold_pct = models.DecimalField(
        "Yuqori o‘lim chegarasi, %", max_digits=4, decimal_places=1, default=Decimal("5.0")
    )
    problem_report_days = models.PositiveSmallIntegerField("Hisobotsiz kunlar chegarasi", default=2)
    low_stock_alert_enabled = models.BooleanField("Kam qoldiq ogohlantirishlari", default=True)
    delivery_proof_required = models.BooleanField("Yetkazib berish dalili majburiy", default=True)
    reminder_hour = models.PositiveSmallIntegerField("Eslatmalar soati", default=8)
    last_reminders_date = models.DateField("Oxirgi eslatmalar sanasi", null=True, blank=True, editable=False)
    updated_at = models.DateTimeField("O‘zgartirilgan", auto_now=True)
    updated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
        verbose_name="O‘zgartirgan",
    )

    class Meta:
        verbose_name = "Kompaniya sozlamalari"
        verbose_name_plural = "Kompaniya sozlamalari"
        constraints = [
            models.CheckConstraint(condition=Q(pk=1), name="system_settings_singleton"),
            models.CheckConstraint(condition=Q(vaccination_reminder_days__lte=30), name="settings_vacc_days_range"),
            models.CheckConstraint(condition=Q(expiry_reminder_days__lte=365), name="settings_expiry_days_range"),
            models.CheckConstraint(
                condition=Q(mortality_alert_threshold_pct__gte=0, mortality_alert_threshold_pct__lte=100),
                name="settings_mortality_range",
            ),
            models.CheckConstraint(
                condition=Q(problem_report_days__gte=1, problem_report_days__lte=30), name="settings_report_days_range"
            ),
            models.CheckConstraint(condition=Q(reminder_hour__lte=23), name="settings_reminder_hour_range"),
        ]

    def save(self, *args, **kwargs) -> None:
        self.pk = 1
        super().save(*args, **kwargs)

    def __str__(self) -> str:
        return self.company_name


class AttachmentPurpose(models.TextChoices):
    DELIVERY_PROOF = "DELIVERY_PROOF", "Yetkazib berish dalili"
    VET_VISIT = "VET_VISIT", "Tekshiruv fayli"
    VACCINATION = "VACCINATION", "Emlash fayli"
    DISEASE = "DISEASE", "Kasallik fayli"
    TREATMENT = "TREATMENT", "Davolash fayli"
    VET_REQUEST = "VET_REQUEST", "Murojaat rasmi"
    # TZ §7 "Har bir kirim va chiqim tegishli hujjat bilan tasdiqlansin": waybill / invoice scan.
    STOCK_DOCUMENT = "STOCK_DOCUMENT", "Ombor hujjati"


def attachment_upload_to(instance: Attachment, filename: str) -> str:
    """``uploads/YYYY/MM/<uuid4 hex>.<ext>`` — the original name is never used as a path."""
    ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else "bin"
    now = timezone.localtime()
    return f"uploads/{now:%Y}/{now:%m}/{uuid.uuid4().hex}.{ext}"


class Attachment(models.Model):
    """An uploaded file; bound exactly once to a business object (``object_type`` + ``object_id``)."""

    file = models.FileField("Fayl", storage=private_storage, upload_to=attachment_upload_to, max_length=255)
    original_name = models.CharField("Asl nomi", max_length=200)
    content_type = models.CharField("Turi", max_length=64)
    size = models.PositiveIntegerField("Hajmi (bayt)")
    sha256 = models.CharField("SHA-256", max_length=64)
    purpose = models.CharField("Maqsad", max_length=32, choices=AttachmentPurpose.choices)
    uploaded_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+", verbose_name="Yuklagan"
    )
    object_type = models.CharField("Obyekt turi", max_length=32, null=True, blank=True)
    object_id = models.BigIntegerField("Obyekt ID", null=True, blank=True)
    household = models.ForeignKey(
        "farm.Household", on_delete=models.PROTECT, null=True, blank=True, related_name="+", verbose_name="Xonadon"
    )
    created_at = models.DateTimeField("Yuklangan", auto_now_add=True)
    bound_at = models.DateTimeField("Biriktirilgan", null=True, blank=True)

    class Meta:
        verbose_name = "Fayl"
        verbose_name_plural = "Fayllar"
        ordering = ("id",)
        indexes = [
            models.Index(fields=["object_type", "object_id"], name="attachment_object_idx"),
            models.Index(fields=["bound_at", "created_at"], name="attachment_unbound_idx"),
        ]
        constraints = [
            models.CheckConstraint(
                condition=Q(object_type__isnull=True, object_id__isnull=True)
                | Q(object_type__isnull=False, object_id__isnull=False),
                name="attachment_bound_pair",
            ),
        ]

    @property
    def is_bound(self) -> bool:
        return self.object_id is not None

    @property
    def url(self) -> str:
        return f"/api/files/{self.pk}"

    def __str__(self) -> str:
        return f"{self.original_name} (#{self.pk})"

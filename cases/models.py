import uuid
from pathlib import Path

from django.conf import settings
from django.db import models
from django.utils import timezone

from . import stages as chain


class CaseStatus(models.TextChoices):
    IN_PROGRESS = "IN_PROGRESS", "Jarayonda"
    DONE = "DONE", "Yakunlangan"
    REJECTED = "REJECTED", "Rad etilgan"


class StageStatus(models.TextChoices):
    WAITING = "WAITING", "Kutilmoqda"
    ACTIVE = "ACTIVE", "Faol"
    SUBMITTED = "SUBMITTED", "Tasdiqlash kutilmoqda"
    RETURNED = "RETURNED", "Qaytarilgan"
    DONE = "DONE", "Bajarildi"


class Case(models.Model):
    """A household (Xonadon) and its way through the chain."""

    code = models.CharField(max_length=20, unique=True, blank=True)
    name = models.CharField("Xonadon nomi", max_length=200)
    owner_full_name = models.CharField("Egasi F.I.Sh.", max_length=200)
    owner_phone = models.CharField("Egasi telefoni", max_length=30, blank=True)
    region = models.CharField("Viloyat", max_length=100, blank=True)
    district = models.CharField("Tuman", max_length=100, blank=True)
    mfy = models.CharField("MFY", max_length=150, blank=True)
    address = models.CharField("Manzil", max_length=300, blank=True)
    latitude = models.DecimalField(max_digits=9, decimal_places=6, null=True, blank=True)
    longitude = models.DecimalField(max_digits=9, decimal_places=6, null=True, blank=True)
    package = models.ForeignKey("stock.Package", on_delete=models.PROTECT, related_name="cases")
    owner_user = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL,
                                   related_name="owned_cases")
    status = models.CharField(max_length=20, choices=CaseStatus.choices, default=CaseStatus.IN_PROGRESS, db_index=True)
    current_stage = models.PositiveSmallIntegerField(default=1, db_index=True)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL,
                                   related_name="created_cases")
    created_at = models.DateTimeField(auto_now_add=True)
    done_at = models.DateTimeField(null=True, blank=True)
    reject_reason = models.TextField(blank=True)

    class Meta:
        ordering = ["-id"]
        verbose_name = "Xonadon"
        verbose_name_plural = "Xonadonlar"

    def __str__(self) -> str:
        return f"{self.code} — {self.name}"


class CaseStage(models.Model):
    case = models.ForeignKey(Case, on_delete=models.CASCADE, related_name="stages")
    number = models.PositiveSmallIntegerField()
    key = models.CharField(max_length=30)
    status = models.CharField(max_length=20, choices=StageStatus.choices, default=StageStatus.WAITING)
    data = models.JSONField(default=dict, blank=True)
    note = models.TextField(blank=True)
    activated_at = models.DateTimeField(null=True, blank=True)
    due_at = models.DateTimeField(null=True, blank=True)
    submitted_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL,
                                     related_name="+")
    submitted_at = models.DateTimeField(null=True, blank=True)
    decided_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL,
                                   related_name="+")
    decided_at = models.DateTimeField(null=True, blank=True)
    return_reason = models.TextField(blank=True)
    overdue_notified_on = models.DateField(null=True, blank=True)

    class Meta:
        ordering = ["case_id", "number"]
        unique_together = [("case", "number")]
        verbose_name = "Bosqich"
        verbose_name_plural = "Bosqichlar"

    def __str__(self) -> str:
        return f"{self.case.code}-{self.number} {self.status}"

    @property
    def definition(self) -> chain.Stage:
        return chain.get(self.number)

    @property
    def is_overdue(self) -> bool:
        return (
            self.status in (StageStatus.ACTIVE, StageStatus.RETURNED)
            and self.due_at is not None
            and self.due_at < timezone.now()
        )


def _file_path(folder: str, filename: str) -> str:
    suffix = Path(filename).suffix.lower()[:10]
    return f"{folder}/{timezone.now():%Y/%m}/{uuid.uuid4().hex}{suffix}"


def photo_path(instance, filename: str) -> str:
    return _file_path("photos", filename)


def document_path(instance, filename: str) -> str:
    return _file_path("documents", filename)


class Photo(models.Model):
    """Uploaded first (case is empty), attached to a case stage by create/submit."""

    case = models.ForeignKey(Case, null=True, blank=True, on_delete=models.CASCADE, related_name="photos")
    stage = models.PositiveSmallIntegerField(null=True, blank=True)
    image = models.ImageField(upload_to=photo_path)
    taken_at = models.DateTimeField(default=timezone.now)
    lat = models.DecimalField(max_digits=9, decimal_places=6, null=True, blank=True)
    lng = models.DecimalField(max_digits=9, decimal_places=6, null=True, blank=True)
    live = models.BooleanField(default=False)
    uploaded_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL, related_name="+")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["id"]
        verbose_name = "Rasm"
        verbose_name_plural = "Rasmlar"


class Document(models.Model):
    case = models.ForeignKey(Case, null=True, blank=True, on_delete=models.CASCADE, related_name="documents")
    stage = models.PositiveSmallIntegerField(null=True, blank=True)
    file = models.FileField(upload_to=document_path)
    original_name = models.CharField(max_length=255)
    uploaded_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL, related_name="+")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["id"]
        verbose_name = "Hujjat"
        verbose_name_plural = "Hujjatlar"


class StageEvent(models.Model):
    CREATE, SUBMIT, APPROVE, RETURN, REJECT = "create", "submit", "approve", "return", "reject"
    ACTIONS = [
        (CREATE, "Yaratildi"),
        (SUBMIT, "Bajarildi"),
        (APPROVE, "Tasdiqlandi"),
        (RETURN, "Qaytarildi"),
        (REJECT, "Rad etildi"),
    ]

    case = models.ForeignKey(Case, on_delete=models.CASCADE, related_name="events")
    stage_number = models.PositiveSmallIntegerField()
    action = models.CharField(max_length=10, choices=ACTIONS)
    actor = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL, related_name="+")
    note = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["id"]
        verbose_name = "Bosqich hodisasi"
        verbose_name_plural = "Bosqich hodisalari"

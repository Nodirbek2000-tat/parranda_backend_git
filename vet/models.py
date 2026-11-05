"""Veterinary (API_CONTRACT_V3 §8): VetVisit, Vaccination, DiseaseRecord, Treatment, VetRequest.

Bird deaths are never stored here: they are ``DEATH`` poultry movements (farm app), so they are counted once.
Vaccine / drug usage lowers the central stock through ``warehouse.services`` (OUT rows linked one-to-one).
"""

from __future__ import annotations

from django.conf import settings
from django.db import models
from django.db.models import F, Q
from django.utils import timezone

from common.softdelete import SoftDeleteManager

MAX_LONG_TEXT = 4000


class VetStatus(models.TextChoices):
    """Status of a visit or a vaccination."""

    PLANNED = "PLANNED", "Rejalashtirilgan"
    DONE = "DONE", "O‘tkazildi"
    CANCELLED = "CANCELLED", "Bekor qilindi"


class HealthStatus(models.TextChoices):
    GOOD = "GOOD", "Yaxshi"
    FAIR = "FAIR", "Qoniqarli"
    POOR = "POOR", "Yomon"
    CRITICAL = "CRITICAL", "Xavfli"


class DiseaseStatus(models.TextChoices):
    OPEN = "OPEN", "Aniqlangan"
    TREATMENT = "TREATMENT", "Davolanmoqda"
    RESOLVED = "RESOLVED", "Tuzaldi"


class RequestStatus(models.TextChoices):
    NEW = "NEW", "Yangi"
    IN_PROGRESS = "IN_PROGRESS", "Ko‘rib chiqilmoqda"
    CLOSED = "CLOSED", "Yopildi"


def _user_fk(verbose_name: str, *, null: bool = False) -> models.ForeignKey:
    return models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="+",
        null=null,
        blank=null,
        verbose_name=verbose_name,
    )


def _household_fk(related_name: str) -> models.ForeignKey:
    return models.ForeignKey(
        "farm.Household", on_delete=models.PROTECT, related_name=related_name, verbose_name="Xonadon"
    )


def _batch_fk() -> models.ForeignKey:
    return models.ForeignKey(
        "farm.PoultryBatch", on_delete=models.PROTECT, related_name="+", null=True, blank=True, verbose_name="Partiya"
    )


class _Coded(models.Model):
    CODE_PREFIX = ""

    class Meta:
        abstract = True

    @property
    def code(self) -> str:
        return f"{self.CODE_PREFIX}-{self.pk:06d}" if self.pk else ""

    def __str__(self) -> str:
        return self.code


class VetRequest(_Coded):
    """Owner request “Veterinarga murojaat” (§8.5)."""

    CODE_PREFIX = "VM"

    household = _household_fk("vet_requests")
    created_by = _user_fk("Yuborgan")
    text = models.TextField("Matn", max_length=2000)
    status = models.CharField("Holat", max_length=16, choices=RequestStatus.choices, default=RequestStatus.NEW)
    vet = _user_fk("Veterinar", null=True)
    response = models.TextField("Javob", max_length=MAX_LONG_TEXT, blank=True)
    closed_at = models.DateTimeField("Yopilgan", null=True, blank=True)
    closed_by = _user_fk("Yopgan", null=True)
    created_at = models.DateTimeField("Yaratilgan", default=timezone.now, editable=False)
    updated_at = models.DateTimeField("Yangilangan", auto_now=True)
    deleted_at = models.DateTimeField("O‘chirilgan vaqt", null=True, blank=True, editable=False)

    objects = SoftDeleteManager()
    all_objects = models.Manager()

    class Meta:
        verbose_name = "Veterinarga murojaat"
        verbose_name_plural = "Veterinarga murojaatlar"
        ordering = ("-created_at", "-id")
        indexes = [
            models.Index(fields=["status", "created_at"], name="vet_request_status_idx"),
            models.Index(fields=["household", "status"], name="vet_request_household_idx"),
        ]
        constraints = [
            models.CheckConstraint(
                condition=~Q(status=RequestStatus.CLOSED) | Q(closed_at__isnull=False),
                name="vet_request_closed_has_time",
            ),
        ]


class VetVisit(_Coded):
    """Planned / done inspection with the vet's conclusion (§8.1)."""

    CODE_PREFIX = "VT"

    household = _household_fk("vet_visits")
    batch = _batch_fk()
    vet = _user_fk("Veterinar", null=True)
    planned_date = models.DateField("Reja sanasi")
    purpose = models.CharField("Maqsad", max_length=500)
    status = models.CharField("Holat", max_length=16, choices=VetStatus.choices, default=VetStatus.PLANNED)
    done_at = models.DateTimeField("O‘tkazilgan vaqt", null=True, blank=True)
    birds_checked = models.PositiveIntegerField("Tekshirilgan parranda", null=True, blank=True)
    health_status = models.CharField("Sog‘liq holati", max_length=16, choices=HealthStatus.choices, blank=True)
    findings = models.TextField("Aniqlanganlar", max_length=MAX_LONG_TEXT, blank=True)
    conclusion = models.TextField("Xulosa", max_length=MAX_LONG_TEXT, blank=True)
    recommendations = models.TextField("Tavsiyalar", max_length=MAX_LONG_TEXT, blank=True)
    request = models.ForeignKey(
        VetRequest, on_delete=models.PROTECT, related_name="visits", null=True, blank=True, verbose_name="Murojaat"
    )
    cancel_reason = models.CharField("Bekor qilish sababi", max_length=500, blank=True)
    created_by = _user_fk("Kiritgan", null=True)
    created_at = models.DateTimeField("Yaratilgan", default=timezone.now, editable=False)
    updated_at = models.DateTimeField("Yangilangan", auto_now=True)
    deleted_at = models.DateTimeField("O‘chirilgan vaqt", null=True, blank=True, editable=False)

    objects = SoftDeleteManager()
    all_objects = models.Manager()

    class Meta:
        verbose_name = "Veterinariya tekshiruvi"
        verbose_name_plural = "Veterinariya tekshiruvlari"
        ordering = ("planned_date", "id")
        indexes = [
            models.Index(fields=["status", "planned_date"], name="vet_visit_status_idx"),
            models.Index(fields=["household", "planned_date"], name="vet_visit_household_idx"),
            models.Index(fields=["vet", "status"], name="vet_visit_vet_idx"),
        ]
        constraints = [
            models.CheckConstraint(
                condition=~Q(status=VetStatus.DONE)
                | (Q(done_at__isnull=False) & ~Q(health_status="") & ~Q(conclusion="")),
                name="vet_visit_done_complete",
            ),
        ]

    @property
    def is_overdue(self) -> bool:
        return self.status == VetStatus.PLANNED and self.planned_date < timezone.localdate()


class Vaccination(_Coded):
    """Vaccination calendar entry and its result (§8.2)."""

    CODE_PREFIX = "EM"

    household = _household_fk("vaccinations")
    batch = _batch_fk()
    vet = _user_fk("Veterinar", null=True)
    product = models.ForeignKey(
        "warehouse.Product", on_delete=models.PROTECT, related_name="+", null=True, blank=True, verbose_name="Vaksina"
    )
    vaccine_name = models.CharField("Vaksina nomi", max_length=200)
    dose = models.CharField("Doza", max_length=100, blank=True)
    planned_date = models.DateField("Reja sanasi")
    birds_planned = models.PositiveIntegerField("Rejadagi parranda", null=True, blank=True)
    status = models.CharField("Holat", max_length=16, choices=VetStatus.choices, default=VetStatus.PLANNED)
    done_date = models.DateField("O‘tkazilgan sana", null=True, blank=True)
    birds_vaccinated = models.PositiveIntegerField("Emlangan parranda", null=True, blank=True)
    quantity_used = models.DecimalField("Sarflangan miqdor", max_digits=12, decimal_places=2, null=True, blank=True)
    result = models.TextField("Natija", max_length=2000, blank=True)
    note = models.CharField("Izoh", max_length=500, blank=True)
    stock_transaction = models.OneToOneField(
        "warehouse.StockTransaction",
        on_delete=models.PROTECT,
        related_name="vaccination",
        null=True,
        blank=True,
        verbose_name="Ombor chiqimi",
    )
    cancel_reason = models.CharField("Bekor qilish sababi", max_length=500, blank=True)
    created_by = _user_fk("Kiritgan", null=True)
    created_at = models.DateTimeField("Yaratilgan", default=timezone.now, editable=False)
    updated_at = models.DateTimeField("Yangilangan", auto_now=True)
    deleted_at = models.DateTimeField("O‘chirilgan vaqt", null=True, blank=True, editable=False)

    objects = SoftDeleteManager()
    all_objects = models.Manager()

    class Meta:
        verbose_name = "Emlash"
        verbose_name_plural = "Emlashlar"
        ordering = ("planned_date", "id")
        indexes = [
            models.Index(fields=["status", "planned_date"], name="vaccination_status_idx"),
            models.Index(fields=["household", "planned_date"], name="vaccination_household_idx"),
            models.Index(fields=["batch", "planned_date"], name="vaccination_batch_idx"),
        ]
        constraints = [
            models.CheckConstraint(
                condition=~Q(status=VetStatus.DONE) | Q(done_date__isnull=False), name="vaccination_done_has_date"
            ),
            models.CheckConstraint(
                condition=Q(quantity_used__isnull=True) | Q(quantity_used__gte=0), name="vaccination_quantity_gte_0"
            ),
            models.CheckConstraint(
                condition=Q(birds_vaccinated__isnull=True) | Q(birds_vaccinated__gt=0),
                name="vaccination_birds_gt_0",
            ),
        ]

    @property
    def is_overdue(self) -> bool:
        return self.status == VetStatus.PLANNED and self.planned_date < timezone.localdate()


class DiseaseRecord(_Coded):
    """Disease registry entry (§8.3)."""

    CODE_PREFIX = "KS"

    household = _household_fk("disease_records")
    batch = _batch_fk()
    vet = _user_fk("Veterinar", null=True)
    visit = models.ForeignKey(
        VetVisit, on_delete=models.PROTECT, related_name="diseases", null=True, blank=True, verbose_name="Tekshiruv"
    )
    disease_name = models.CharField("Kasallik", max_length=200)
    diagnosed_date = models.DateField("Aniqlangan sana")
    cases = models.PositiveIntegerField("Holatlar soni")
    symptoms = models.TextField("Belgilar", max_length=2000, blank=True)
    status = models.CharField("Holat", max_length=16, choices=DiseaseStatus.choices, default=DiseaseStatus.OPEN)
    resolved_date = models.DateField("Tuzalgan sana", null=True, blank=True)
    notes = models.TextField("Izoh", max_length=2000, blank=True)
    created_by = _user_fk("Kiritgan", null=True)
    created_at = models.DateTimeField("Yaratilgan", default=timezone.now, editable=False)
    updated_at = models.DateTimeField("Yangilangan", auto_now=True)
    deleted_at = models.DateTimeField("O‘chirilgan vaqt", null=True, blank=True, editable=False)

    objects = SoftDeleteManager()
    all_objects = models.Manager()

    class Meta:
        verbose_name = "Kasallik qaydi"
        verbose_name_plural = "Kasalliklar reyestri"
        ordering = ("-diagnosed_date", "-id")
        indexes = [
            models.Index(fields=["status", "diagnosed_date"], name="disease_status_idx"),
            models.Index(fields=["household", "status"], name="disease_household_idx"),
        ]
        constraints = [
            models.CheckConstraint(condition=Q(cases__gt=0), name="disease_cases_gt_0"),
            models.CheckConstraint(
                condition=~Q(status=DiseaseStatus.RESOLVED) | Q(resolved_date__isnull=False),
                name="disease_resolved_has_date",
            ),
            models.CheckConstraint(
                condition=Q(resolved_date__isnull=True) | Q(resolved_date__gte=F("diagnosed_date")),
                name="disease_resolved_after_diagnosis",
            ),
        ]


class Treatment(_Coded):
    """Treatment / recommendation for the owner (§8.4)."""

    CODE_PREFIX = "DV"

    household = _household_fk("treatments")
    batch = _batch_fk()
    disease = models.ForeignKey(
        DiseaseRecord,
        on_delete=models.PROTECT,
        related_name="treatments",
        null=True,
        blank=True,
        verbose_name="Kasallik",
    )
    vet = _user_fk("Veterinar", null=True)
    product = models.ForeignKey(
        "warehouse.Product", on_delete=models.PROTECT, related_name="+", null=True, blank=True, verbose_name="Preparat"
    )
    drug_name = models.CharField("Preparat nomi", max_length=200)
    dosage = models.CharField("Dozasi", max_length=200, blank=True)
    start_date = models.DateField("Boshlanish sanasi")
    end_date = models.DateField("Tugash sanasi", null=True, blank=True)
    quantity_used = models.DecimalField("Sarflangan miqdor", max_digits=12, decimal_places=2, null=True, blank=True)
    instructions = models.TextField("Ko‘rsatmalar", max_length=MAX_LONG_TEXT, blank=True)
    stock_transaction = models.OneToOneField(
        "warehouse.StockTransaction",
        on_delete=models.PROTECT,
        related_name="treatment",
        null=True,
        blank=True,
        verbose_name="Ombor chiqimi",
    )
    created_by = _user_fk("Kiritgan", null=True)
    created_at = models.DateTimeField("Yaratilgan", default=timezone.now, editable=False)
    updated_at = models.DateTimeField("Yangilangan", auto_now=True)

    class Meta:
        verbose_name = "Davolash"
        verbose_name_plural = "Davolashlar"
        ordering = ("-start_date", "-id")
        indexes = [
            models.Index(fields=["household", "start_date"], name="treatment_household_idx"),
        ]
        constraints = [
            models.CheckConstraint(
                condition=Q(end_date__isnull=True) | Q(end_date__gte=F("start_date")), name="treatment_end_after_start"
            ),
            models.CheckConstraint(
                condition=Q(quantity_used__isnull=True) | Q(quantity_used__gte=0), name="treatment_quantity_gte_0"
            ),
        ]

    @property
    def is_active(self) -> bool:
        return self.end_date is None or self.end_date >= timezone.localdate()
